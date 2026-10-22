"""Interchangeable measurement-source boundary for external evidence."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol, runtime_checkable
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, field_validator

from autonomous_lab.domain.base import DomainModel
from autonomous_lab.domain.enums import MeasurementSourceType
from autonomous_lab.domain.identifiers import require_utc, utc_now
from autonomous_lab.domain.protocols import QuantityRange
from autonomous_lab.domain.quantities import QuantityValue
from autonomous_lab.hardware.sensors import (
    CalibrationRecord,
    CalibrationStatus,
    MeasurementRequest,
    MockPhysicalSensor,
    MockSensorFixture,
    MockSensorScenario,
    ReadOnlySensor,
    SensorCalibrationError,
    SensorCapabilityError,
    SensorError,
    SensorNotReadyError,
    SensorReadTimeoutError,
    WavelengthCapabilities,
)
from autonomous_lab.measurement_ingestion.csv_import import parse_measurement_csv
from autonomous_lab.measurement_ingestion.models import (
    ExpectedMeasurementPlan,
    MeasurementAvailability,
    MeasurementBatchKind,
    MeasurementImportResult,
    RawMeasurement,
    RawMeasurementBatch,
    make_raw_batch,
)


class MeasurementAcquisitionContext(DomainModel):
    experiment_id: UUID
    run_id: UUID
    submitted_by: str = Field(min_length=1, max_length=128)
    acquired_at: datetime

    @field_validator("acquired_at")
    @classmethod
    def _utc_acquired_at(cls, value: datetime) -> datetime:
        return require_utc(value)


@runtime_checkable
class MeasurementSource(Protocol):
    """Produce one immutable raw batch without performing QC or interpretation."""

    source_type: MeasurementSourceType

    async def acquire(
        self,
        plan: ExpectedMeasurementPlan,
        context: MeasurementAcquisitionContext,
    ) -> RawMeasurementBatch: ...


class ManualMeasurementSource:
    """Adapter for a previously finalized manual-entry batch."""

    source_type = MeasurementSourceType.MANUAL_ENTRY

    def __init__(self, batch: RawMeasurementBatch) -> None:
        if batch.source_type is not self.source_type:
            raise ValueError("manual source requires a manual-entry batch")
        self.batch = batch

    async def acquire(
        self,
        plan: ExpectedMeasurementPlan,
        context: MeasurementAcquisitionContext,
    ) -> RawMeasurementBatch:
        _require_identity(self.batch, plan, context)
        return self.batch


class CSVMeasurementSource:
    source_type = MeasurementSourceType.FILE_IMPORT

    def __init__(
        self,
        content: bytes,
        *,
        source_filename: str,
        batch_kind: MeasurementBatchKind = MeasurementBatchKind.INITIAL,
        correction_reason: str | None = None,
    ) -> None:
        self.content = bytes(content)
        self.source_filename = source_filename
        self.batch_kind = batch_kind
        self.correction_reason = correction_reason

    async def acquire(
        self,
        plan: ExpectedMeasurementPlan,
        context: MeasurementAcquisitionContext,
    ) -> RawMeasurementBatch:
        result = parse_measurement_csv(
            self.content,
            plan=plan,
            submitted_by=context.submitted_by,
            source_filename=self.source_filename,
            batch_kind=self.batch_kind,
            correction_reason=self.correction_reason,
            submitted_at=context.acquired_at,
        )
        return _accepted(result)


class SimulatedMeasurementSource:
    """Typed seam for already-created simulated evidence; never calls a simulator."""

    source_type = MeasurementSourceType.SIMULATED

    def __init__(self, batch: RawMeasurementBatch) -> None:
        if batch.source_type is not self.source_type:
            raise ValueError("simulated source requires explicitly simulated evidence")
        self.batch = batch

    async def acquire(
        self,
        plan: ExpectedMeasurementPlan,
        context: MeasurementAcquisitionContext,
    ) -> RawMeasurementBatch:
        _require_identity(self.batch, plan, context)
        return self.batch


class ReadOnlySensorSource:
    """Acquire a raw batch through any conforming read-only sensor adapter."""

    source_type = MeasurementSourceType.PHYSICAL_SENSOR

    def __init__(
        self,
        sensor: ReadOnlySensor,
        *,
        source_metadata: dict[str, object] | None = None,
    ) -> None:
        self.sensor = sensor
        self.source_metadata = dict(source_metadata or {})

    async def acquire(
        self,
        plan: ExpectedMeasurementPlan,
        context: MeasurementAcquisitionContext,
    ) -> RawMeasurementBatch:
        if context.experiment_id != plan.experiment_id:
            raise ValueError("acquisition context does not match measurement plan")
        connection = await self.sensor.open()
        try:
            capabilities = await self.sensor.capability_manifest()
            if capabilities.sensor_id != self.sensor.sensor_id:
                raise SensorError(
                    "sensor_capability_identity_mismatch",
                    "sensor capability identity changed during acquisition",
                )
            calibration_by_type = {
                item.measurement_type: item for item in capabilities.calibrations
            }
            if capabilities.calibration_required:
                for measurement_type in {item.measurement_type for item in plan.entries}:
                    calibration = calibration_by_type.get(measurement_type)
                    if calibration is None:
                        raise SensorCalibrationError(
                            "calibration_missing",
                            f"sensor has no calibration for {measurement_type!r}",
                        )
                    if calibration.status is not CalibrationStatus.VALID:
                        raise SensorCalibrationError(
                            f"calibration_{calibration.status.value}",
                            f"sensor calibration is {calibration.status.value}",
                        )
            self_test = await self.sensor.self_test()
            if self_test.sensor_id != self.sensor.sensor_id:
                raise SensorError(
                    "sensor_self_test_identity_mismatch",
                    "sensor self-test identity changed during acquisition",
                )
            if self_test.status.value != "passed":
                raise SensorNotReadyError("sensor self-test failed")
            readings: list[RawMeasurement] = []
            for expected in plan.entries:
                if expected.measurement_type not in capabilities.measurement_types:
                    raise SensorCapabilityError(
                        f"sensor does not support {expected.measurement_type!r}"
                    )
                allowed_range = capabilities.measurement_ranges[
                    expected.measurement_type
                ]
                try:
                    allowed_minimum = allowed_range.minimum.to(expected.unit).value
                    allowed_maximum = allowed_range.maximum.to(expected.unit).value
                except ValueError as exc:
                    raise SensorCapabilityError(
                        f"sensor range is incompatible with requested unit {expected.unit!r}"
                    ) from exc
                if expected.wavelength is not None and (
                    capabilities.wavelength is None
                    or not capabilities.wavelength.supports(expected.wavelength)
                ):
                    raise SensorCapabilityError(
                        "sensor does not support the requested wavelength"
                    )
                request = MeasurementRequest(
                    experiment_id=context.experiment_id,
                    run_id=context.run_id,
                    sample_id=expected.sample_id,
                    measurement_type=expected.measurement_type,
                    expected_unit=expected.unit,
                    wavelength=expected.wavelength,
                    requested_at=context.acquired_at,
                )
                try:
                    result = await asyncio.wait_for(
                        self.sensor.read(request),
                        timeout=float(request.timeout_seconds),
                    )
                except SensorReadTimeoutError:
                    raise
                except TimeoutError as exc:
                    raise SensorReadTimeoutError() from exc
                if (
                    result.request_id != request.request_id
                    or result.experiment_id != context.experiment_id
                    or result.run_id != context.run_id
                    or result.sample_id != expected.sample_id
                    or result.measurement_type != expected.measurement_type
                ):
                    raise SensorError(
                        "sensor_response_identity_mismatch",
                        "sensor response identity does not match its measurement request",
                    )
                evidence = result.source_evidence
                if (
                    evidence.sensor_id != self.sensor.sensor_id
                    or evidence.connection_session_id != connection.session_id
                    or evidence.self_test_id != self_test.self_test_id
                ):
                    raise SensorError(
                        "sensor_lifecycle_evidence_mismatch",
                        "sensor response lifecycle evidence is inconsistent",
                    )
                if evidence.capability_sha256 != capabilities.sha256:
                    raise SensorError(
                        "sensor_capability_hash_mismatch",
                        "sensor response capability hash changed during acquisition",
                    )
                if capabilities.calibration_required:
                    calibration = calibration_by_type[expected.measurement_type]
                    if (
                        evidence.calibration_id != calibration.calibration_id
                        or evidence.calibration_sha256
                        != calibration.calibration_sha256
                    ):
                        raise SensorCalibrationError(
                            "calibration_evidence_mismatch",
                            "sensor response calibration differs from its capability snapshot",
                        )
                if result.unit != expected.unit:
                    raise SensorError(
                        "sensor_response_unit_mismatch",
                        "sensor response unit differs from its request",
                    )
                expected_wavelength = expected.wavelength
                if (result.wavelength is None) != (expected_wavelength is None) or (
                    result.wavelength is not None
                    and expected_wavelength is not None
                    and result.wavelength.to("nanometer").value
                    != expected_wavelength.to("nanometer").value
                ):
                    raise SensorError(
                        "sensor_response_wavelength_mismatch",
                        "sensor response wavelength differs from its request",
                    )
                if not allowed_minimum <= result.raw_value <= allowed_maximum:
                    raise SensorError(
                        "sensor_response_out_of_range",
                        "sensor response is outside its frozen capability range",
                    )
                readings.append(
                    RawMeasurement(
                        expected_measurement_id=expected.expected_measurement_id,
                        sample_id=result.sample_id,
                        measurement_type=result.measurement_type,
                        wavelength=result.wavelength,
                        availability=MeasurementAvailability.AVAILABLE,
                        value=result.raw_value,
                        unit=result.unit,
                        measured_at=result.measured_at,
                        source_metadata={
                            "device_id": result.source_evidence.sensor_id,
                            "raw_measurement_id": str(result.raw_measurement_id),
                            "sensor_capability_manifest": capabilities.model_dump(
                                mode="json"
                            ),
                            "sensor_capability_sha256": capabilities.sha256,
                            "sensor_self_test": self_test.model_dump(mode="json"),
                            "sensor_evidence": evidence.model_dump(mode="json"),
                            "sensor_response_metadata": result.metadata,
                            **self.source_metadata,
                        },
                    )
                )
        finally:
            if self.sensor.is_open:
                await self.sensor.close()
        return make_raw_batch(
            experiment_id=plan.experiment_id,
            plan_id=plan.plan_id,
            source_type=self.source_type,
            measurements=tuple(readings),
            submitted_by=context.submitted_by,
            submitted_at=context.acquired_at,
        )


# Compatibility name retained for existing V0.5 callers. The implementation is
# generic and accepts any ReadOnlySensor, not only the mock fixture adapter.
MockPhysicalSensorSource = ReadOnlySensorSource


def mock_physical_sensor_source_from_rows(
    sensor_id: str,
    rows: tuple[RawMeasurement, ...],
    *,
    fixture_name: str,
    fixture_sha256: str,
    now: datetime | None = None,
) -> ReadOnlySensorSource:
    """Build the explicit zero-hardware sensor boundary from external fixture rows."""

    if not rows:
        raise ValueError("mock optical sensor requires at least one fixture row")
    if not fixture_name.strip() or len(fixture_sha256) != 64:
        raise ValueError("mock sensor fixture name and SHA-256 are required")
    at = now or datetime.now(UTC)
    measurement_types = {item.measurement_type for item in rows}
    if len(measurement_types) != 1:
        raise ValueError("V0.5 mock optical sensor requires one measurement type")
    measurement_type = next(iter(measurement_types))
    wavelength_by_value = {
        item.wavelength.to("nanometer").value: item.wavelength
        for item in rows
        if item.wavelength is not None
    }
    wavelengths = tuple(wavelength_by_value[key] for key in sorted(wavelength_by_value))
    calibration = CalibrationRecord(
        calibration_id=uuid5(NAMESPACE_URL, f"autolab:{sensor_id}:mock-calibration:1"),
        sensor_id=sensor_id,
        measurement_type=measurement_type,
        calibrated_at=at - timedelta(days=1),
        expires_at=at + timedelta(days=365),
        method="zero-hardware fixture calibration",
        reference_material="mock certified reference",
        coefficients={"slope": Decimal("1"), "intercept": Decimal("0")},
        notes="Mock-only calibration; never evidence of a physical calibration.",
    )
    sensor = MockPhysicalSensor(
        sensor_id=sensor_id,
        scenario=MockSensorScenario(
            fixtures=tuple(
                MockSensorFixture(
                    sample_id=item.sample_id,
                    measurement_type=item.measurement_type,
                    value=item.value,
                    unit=item.unit,
                    wavelength=item.wavelength,
                    metadata={
                        "fixture_only": True,
                        "fixture_name": fixture_name,
                        "fixture_sha256": fixture_sha256,
                    },
                )
                for item in rows
                if item.value is not None
            )
        ),
        measurement_ranges={
            measurement_type: QuantityRange(
                minimum=QuantityValue(value=Decimal("-1"), unit=rows[0].unit),
                maximum=QuantityValue(value=Decimal("10"), unit=rows[0].unit),
                resolution=QuantityValue(value=Decimal("0.0001"), unit=rows[0].unit),
            )
        },
        wavelength=(
            None
            if not wavelengths
            else WavelengthCapabilities(discrete_wavelengths=wavelengths)
        ),
        calibration=calibration,
    )
    return ReadOnlySensorSource(
        sensor,
        source_metadata={
            "fixture_only": True,
            "fixture_name": fixture_name,
            "fixture_sha256": fixture_sha256,
            "calibration_record": calibration.model_dump(mode="json"),
            "calibration_record_sha256": calibration.sha256,
        },
    )


def acquisition_context(
    *,
    experiment_id: UUID,
    run_id: UUID,
    submitted_by: str,
    acquired_at: datetime | None = None,
) -> MeasurementAcquisitionContext:
    return MeasurementAcquisitionContext(
        experiment_id=experiment_id,
        run_id=run_id,
        submitted_by=submitted_by,
        acquired_at=acquired_at or utc_now(),
    )


def _require_identity(
    batch: RawMeasurementBatch,
    plan: ExpectedMeasurementPlan,
    context: MeasurementAcquisitionContext,
) -> None:
    if batch.experiment_id != plan.experiment_id or batch.plan_id != plan.plan_id:
        raise ValueError("measurement batch does not match expected plan")
    if context.experiment_id != plan.experiment_id:
        raise ValueError("acquisition context does not match expected plan")
    if batch.submitted_by != context.submitted_by:
        raise ValueError("measurement source submitter does not match acquisition context")


def _accepted(result: MeasurementImportResult) -> RawMeasurementBatch:
    if result.batch is None:
        summary = ", ".join(item.code for item in result.issues)
        raise ValueError(f"measurement source was rejected: {summary}")
    return result.batch
