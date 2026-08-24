import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from autonomous_lab.domain.protocols import QuantityRange
from autonomous_lab.domain.quantities import QuantityValue
from autonomous_lab.hardware.sensors import (
    CalibrationRecord,
    MeasurementRequest,
    MockPhysicalSensor,
    MockSensorFixture,
    MockSensorScenario,
    ReadOnlySensor,
    SelfTestStatus,
    SensorCalibrationError,
    SensorCapabilityError,
    SensorConnectionError,
    SensorMalformedResponseError,
    SensorNotReadyError,
    SensorReadTimeoutError,
    WavelengthCapabilities,
    fixed_clock,
)

NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)


def q(value: str, unit: str) -> QuantityValue:
    return QuantityValue(value=Decimal(value), unit=unit)


def optical_sensor(
    *,
    scenario: MockSensorScenario | None = None,
    calibration_expiry: datetime | None = None,
) -> MockPhysicalSensor:
    selected = scenario or MockSensorScenario(
        fixtures=(
            MockSensorFixture(
                sample_id="B01",
                measurement_type="absorbance",
                value=Decimal("0.031"),
                unit="dimensionless",
                wavelength=q("520", "nanometer"),
                metadata={"fixture": "plate-17"},
            ),
        )
    )
    calibration = CalibrationRecord(
        sensor_id="mock-optical-1",
        measurement_type="absorbance",
        calibrated_at=NOW - timedelta(days=1),
        expires_at=calibration_expiry or NOW + timedelta(days=30),
        method="mock reference",
        reference_material="mock-neutral-density",
        coefficients={"slope": Decimal("1")},
        created_at=NOW - timedelta(days=1),
    )
    return MockPhysicalSensor(
        sensor_id="mock-optical-1",
        scenario=selected,
        measurement_ranges={
            "absorbance": QuantityRange(
                minimum=q("0", "dimensionless"),
                maximum=q("3", "dimensionless"),
                resolution=q("0.001", "dimensionless"),
            )
        },
        calibration=calibration,
        wavelength=WavelengthCapabilities(
            range=QuantityRange(
                minimum=q("400", "nanometer"),
                maximum=q("700", "nanometer"),
                resolution=q("1", "nanometer"),
            )
        ),
        resolutions={"absorbance": q("0.001", "dimensionless")},
        precisions={"absorbance": q("0.002", "dimensionless")},
        clock=fixed_clock(NOW),
    )


def request(*, sample_id: str = "B01", wavelength: str = "520") -> MeasurementRequest:
    return MeasurementRequest(
        experiment_id=uuid4(),
        run_id=uuid4(),
        sample_id=sample_id,
        measurement_type="absorbance",
        expected_unit="dimensionless",
        wavelength=q(wavelength, "nanometer"),
        timeout_seconds=Decimal("5"),
        requested_at=NOW,
    )


def ready(sensor: MockPhysicalSensor) -> None:
    asyncio.run(sensor.open())
    result = asyncio.run(sensor.self_test())
    assert result.status is SelfTestStatus.PASSED


def test_mock_sensor_reports_typed_capabilities_and_satisfies_contract() -> None:
    sensor = optical_sensor()

    assert isinstance(sensor, ReadOnlySensor)
    manifest = asyncio.run(sensor.capability_manifest())

    assert manifest.measurement_types == frozenset({"absorbance"})
    assert manifest.wavelength is not None
    assert manifest.calibrations[0].status.value == "valid"
    assert manifest.metadata == {"external_boundary": True, "is_mock": True}
    assert len(manifest.sha256) == 64


def test_explicit_connection_and_self_test_are_required() -> None:
    sensor = optical_sensor()
    with pytest.raises(SensorConnectionError):
        asyncio.run(sensor.read(request()))

    opened = asyncio.run(sensor.open())
    assert sensor.is_open
    with pytest.raises(SensorNotReadyError):
        asyncio.run(sensor.read(request()))

    asyncio.run(sensor.self_test())
    closed = asyncio.run(sensor.close())
    assert closed.session_id == opened.session_id
    assert not sensor.is_open


def test_successful_reads_are_deterministic_and_have_physical_boundary_evidence() -> None:
    sensor = optical_sensor()
    ready(sensor)
    read_request = request()

    first = asyncio.run(sensor.read(read_request))
    second = asyncio.run(sensor.read(read_request))

    assert first.raw_value == second.raw_value == Decimal("0.031")
    assert first.unit == "dimensionless"
    assert first.source_evidence.source_type == "physical_sensor"
    assert first.source_evidence.is_mock
    assert first.source_evidence.sensor_id == sensor.sensor_id
    assert first.source_evidence.calibration_id is not None
    assert first.metadata == {"fixture": "plate-17"}


@pytest.mark.parametrize(
    ("field", "error"),
    [
        ("timeout_sample_ids", SensorReadTimeoutError),
        ("malformed_sample_ids", SensorMalformedResponseError),
        ("disconnect_sample_ids", SensorConnectionError),
    ],
)
def test_read_failure_scenarios(
    field: str,
    error: type[Exception],
) -> None:
    fixture = MockSensorFixture(
        sample_id="B01",
        measurement_type="absorbance",
        value=Decimal("0.031"),
        unit="dimensionless",
        wavelength=q("520", "nanometer"),
    )
    scenario = MockSensorScenario.model_validate(
        {"fixtures": (fixture,), field: frozenset({"B01"})}
    )
    sensor = optical_sensor(scenario=scenario)
    ready(sensor)

    with pytest.raises(error):
        asyncio.run(sensor.read(request()))


def test_self_test_failure_and_expired_calibration_block_reads() -> None:
    fixture = MockSensorFixture(
        sample_id="B01",
        measurement_type="absorbance",
        value=Decimal("0.031"),
        unit="dimensionless",
        wavelength=q("520", "nanometer"),
    )
    failed_sensor = optical_sensor(
        scenario=MockSensorScenario(fixtures=(fixture,), self_test_fails=True)
    )
    asyncio.run(failed_sensor.open())
    result = asyncio.run(failed_sensor.self_test())
    assert result.status is SelfTestStatus.FAILED
    with pytest.raises(SensorNotReadyError):
        asyncio.run(failed_sensor.read(request()))

    expired = optical_sensor(calibration_expiry=NOW - timedelta(seconds=1))
    ready(expired)
    with pytest.raises(SensorCalibrationError) as raised:
        asyncio.run(expired.read(request()))
    assert raised.value.code == "calibration_expired"


def test_wrong_wavelength_is_rejected_before_fixture_lookup() -> None:
    sensor = optical_sensor()
    ready(sensor)

    with pytest.raises(SensorCapabilityError, match="unsupported"):
        asyncio.run(sensor.read(request(wavelength="900")))


def test_out_of_range_fixture_is_treated_as_malformed_device_data() -> None:
    sensor = optical_sensor(
        scenario=MockSensorScenario(
            fixtures=(
                MockSensorFixture(
                    sample_id="B01",
                    measurement_type="absorbance",
                    value=Decimal("9.9"),
                    unit="dimensionless",
                    wavelength=q("520", "nanometer"),
                ),
            )
        )
    )
    ready(sensor)

    with pytest.raises(SensorMalformedResponseError, match="outside"):
        asyncio.run(sensor.read(request()))


def test_non_optical_sensor_does_not_require_wavelength() -> None:
    sensor = MockPhysicalSensor(
        sensor_id="mock-balance-1",
        scenario=MockSensorScenario(
            fixtures=(
                MockSensorFixture(
                    sample_id="tube-1",
                    measurement_type="mass",
                    value=Decimal("1.234"),
                    unit="gram",
                ),
            )
        ),
        measurement_ranges={
            "mass": QuantityRange(
                minimum=q("0", "gram"),
                maximum=q("10", "gram"),
            )
        },
        calibration=None,
        wavelength=None,
        calibration_required=False,
        clock=fixed_clock(NOW),
    )
    ready(sensor)
    result = asyncio.run(
        sensor.read(
            MeasurementRequest(
                experiment_id=uuid4(),
                run_id=uuid4(),
                sample_id="tube-1",
                measurement_type="mass",
                expected_unit="milligram",
                requested_at=NOW,
            )
        )
    )

    assert result.raw_value == Decimal("1234.000")
    assert result.wavelength is None
    assert result.source_evidence.calibration_id is None
