from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from autonomous_lab.domain.enums import MeasurementRole
from autonomous_lab.domain.quantities import QuantityValue
from autonomous_lab.hardware.sensors import (
    SensorAdapterError,
    SensorAdapterRegistry,
    SensorReadTimeoutError,
)
from autonomous_lab.measurement_ingestion import (
    ExpectedMeasurement,
    RawMeasurement,
    ReadOnlySensorSource,
    acquisition_context,
    make_expected_plan,
    mock_physical_sensor_source_from_rows,
)

NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
EXPERIMENT_ID = UUID("00000000-0000-0000-0000-000000000901")
PROTOCOL_ID = UUID("00000000-0000-0000-0000-000000000902")
RUN_ID = UUID("00000000-0000-0000-0000-000000000903")


def _plan():
    return make_expected_plan(
        experiment_id=EXPERIMENT_ID,
        protocol_id=PROTOCOL_ID,
        protocol_hash="b" * 64,
        entries=(
            ExpectedMeasurement(
                expected_measurement_id="measure:A01",
                sample_id="A01",
                vessel_id="A01",
                step_id="measure",
                sample_role=MeasurementRole.BLANK,
                measurement_type="absorbance",
                unit="absorbance_unit",
                wavelength=QuantityValue(value=520, unit="nanometer"),
                replicate_index=0,
            ),
        ),
        created_at=NOW,
    )


def _source():
    row = RawMeasurement(
        expected_measurement_id="measure:A01",
        sample_id="A01",
        measurement_type="absorbance",
        wavelength=QuantityValue(value=520, unit="nanometer"),
        value=Decimal("0.031"),
        unit="absorbance_unit",
        measured_at=NOW,
    )
    return mock_physical_sensor_source_from_rows(
        "sensor-1",
        (row,),
        fixture_name="fixture.csv",
        fixture_sha256="a" * 64,
        now=NOW,
    )


def test_registry_injects_adapter_without_campaign_or_analysis_changes() -> None:
    expected = _source().sensor
    registry = SensorAdapterRegistry()
    registry.register(
        "example-optical",
        lambda *, sensor_id, settings: expected,
    )

    resolved = registry.create(
        "example-optical",
        sensor_id="sensor-1",
        settings={"port": "/dev/example"},
    )

    assert resolved is expected
    with pytest.raises(SensorAdapterError, match="not installed") as error:
        registry.create("missing", sensor_id="sensor-1")
    assert error.value.code == "sensor_adapter_unavailable"


def test_read_only_source_wraps_plain_timeouts_and_closes_sensor() -> None:
    source = _source()
    sensor = source.sensor

    async def timeout(_request):
        raise TimeoutError("transport timeout")

    sensor.read = timeout  # type: ignore[method-assign]
    context = acquisition_context(
        experiment_id=EXPERIMENT_ID,
        run_id=RUN_ID,
        submitted_by="operator",
        acquired_at=NOW,
    )
    with pytest.raises(SensorReadTimeoutError):
        import asyncio

        asyncio.run(ReadOnlySensorSource(sensor).acquire(_plan(), context))
    assert not sensor.is_open


def test_mock_source_freezes_full_calibration_and_capability_evidence() -> None:
    import asyncio

    source = _source()
    batch = asyncio.run(
        source.acquire(
            _plan(),
            acquisition_context(
                experiment_id=EXPERIMENT_ID,
                run_id=RUN_ID,
                submitted_by="operator",
                acquired_at=NOW,
            ),
        )
    )
    metadata = batch.measurements[0].source_metadata
    assert metadata["calibration_record_sha256"] == metadata["sensor_evidence"][
        "calibration_sha256"
    ]
    assert metadata["calibration_record"]["sensor_id"] == "sensor-1"
    assert metadata["sensor_capability_manifest"]["sensor_id"] == "sensor-1"
