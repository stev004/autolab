from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from autonomous_lab.domain.enums import MeasurementRole
from autonomous_lab.domain.quantities import QuantityValue
from autonomous_lab.hardware.sensors import (
    SensorAdapterError,
    SensorAdapterRegistry,
    SensorCalibrationError,
    SensorError,
    SensorMalformedResponseError,
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
    assert (
        metadata["calibration_record_sha256"] == metadata["sensor_evidence"]["calibration_sha256"]
    )
    assert metadata["calibration_record"]["sensor_id"] == "sensor-1"
    assert metadata["sensor_capability_manifest"]["sensor_id"] == "sensor-1"


@pytest.mark.parametrize("during_read", [False, True])
def test_generic_source_checks_calibration_expiry_at_each_reading(
    monkeypatch: pytest.MonkeyPatch, during_read: bool
) -> None:
    import asyncio

    source = _source()
    sensor = source.sensor
    manifest = sensor.capability_manifest
    read = sensor.read

    async def stale_manifest():
        capabilities = await manifest()
        reference = capabilities.calibrations[0].model_copy(
            update={"expires_at": NOW + timedelta(seconds=1 if during_read else -1)}
        )
        return capabilities.model_copy(update={"calibrations": (reference,)})

    async def later_read(request):
        result = await read(request)
        later = NOW + timedelta(seconds=2)
        evidence = result.source_evidence.model_copy(update={"acquired_at": later})
        return result.model_copy(update={"measured_at": later, "source_evidence": evidence})

    monkeypatch.setattr(sensor, "capability_manifest", stale_manifest)
    if during_read:
        monkeypatch.setattr(sensor, "read", later_read)
    with pytest.raises(SensorCalibrationError) as error:
        asyncio.run(source.acquire(_plan(), _context()))
    assert error.value.code == "calibration_expired"
    assert not sensor.is_open


def _context():
    return acquisition_context(
        experiment_id=EXPERIMENT_ID,
        run_id=RUN_ID,
        submitted_by="operator",
        acquired_at=NOW,
    )


def test_generic_source_rejects_calibration_from_the_future(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    source = _source()
    manifest = source.sensor.capability_manifest

    async def future_manifest():
        capabilities = await manifest()
        reference = capabilities.calibrations[0].model_copy(
            update={"calibrated_at": NOW + timedelta(seconds=1)}
        )
        return capabilities.model_copy(update={"calibrations": (reference,)})

    monkeypatch.setattr(source.sensor, "capability_manifest", future_manifest)
    with pytest.raises(SensorCalibrationError) as error:
        asyncio.run(source.acquire(_plan(), _context()))
    assert error.value.code == "calibration_not_yet_valid"
    assert not source.sensor.is_open


@pytest.mark.parametrize("wrong_field", ["sensor_id", "status"])
def test_generic_source_rejects_inconsistent_open_response(
    monkeypatch: pytest.MonkeyPatch, wrong_field: str
) -> None:
    import asyncio

    from autonomous_lab.hardware.sensors import ConnectionStatus

    source = _source()
    open_sensor = source.sensor.open

    async def inconsistent_open():
        connection = await open_sensor()
        updates = {
            wrong_field: "another-sensor" if wrong_field == "sensor_id" else ConnectionStatus.CLOSED
        }
        return connection.model_copy(update=updates)

    monkeypatch.setattr(source.sensor, "open", inconsistent_open)
    with pytest.raises(SensorError) as error:
        asyncio.run(source.acquire(_plan(), _context()))
    assert error.value.code == "sensor_connection_mismatch"
    assert not source.sensor.is_open


@pytest.mark.parametrize("operation", ["open", "capability_manifest", "self_test", "close"])
def test_every_sensor_lifecycle_call_has_a_deadline(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    import asyncio

    sensor = _source().sensor
    original_open = sensor.open
    original_close = sensor.close

    async def never_returns():
        if operation == "open":
            await original_open()  # Include partially opened transport cleanup.
        await asyncio.Event().wait()

    monkeypatch.setattr(sensor, operation, never_returns)

    async def exercise():
        source = ReadOnlySensorSource(sensor, lifecycle_timeout_seconds=0.01)
        try:
            with pytest.raises(SensorError) as error:
                await asyncio.wait_for(source.acquire(_plan(), _context()), timeout=1)
            assert error.value.code == f"sensor_{operation}_timeout"
            if operation != "close":
                assert not sensor.is_open
        finally:
            if sensor.is_open:
                await original_close()

    asyncio.run(exercise())


def test_cleanup_timeout_preserves_the_original_sensor_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    sensor = _source().sensor
    original_close = sensor.close

    async def malformed(_request):
        raise SensorMalformedResponseError("bad response")

    async def never_closes():
        await asyncio.Event().wait()

    monkeypatch.setattr(sensor, "read", malformed)
    monkeypatch.setattr(sensor, "close", never_closes)

    async def exercise():
        try:
            source = ReadOnlySensorSource(sensor, lifecycle_timeout_seconds=0.01)
            with pytest.raises(SensorMalformedResponseError) as error:
                await source.acquire(_plan(), _context())
            assert any("close timed out" in note for note in error.value.__notes__)
        finally:
            if sensor.is_open:
                await original_close()

    asyncio.run(exercise())
