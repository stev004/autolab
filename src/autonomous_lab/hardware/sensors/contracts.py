"""Narrow vendor-neutral contract for read-only physical instruments."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from autonomous_lab.domain.base import DomainModel
from autonomous_lab.domain.enums import MeasurementSourceType
from autonomous_lab.domain.identifiers import new_uuid, require_utc, utc_now
from autonomous_lab.domain.protocols import QuantityRange
from autonomous_lab.domain.quantities import FiniteDecimal, QuantityValue, validate_unit
from autonomous_lab.hardware.sensors.calibration import CalibrationReference
from autonomous_lab.provenance.hashing import canonical_sha256


class ConnectionStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class SelfTestStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"


class SensorConnection(DomainModel):
    session_id: UUID = Field(default_factory=new_uuid)
    sensor_id: str
    status: ConnectionStatus
    changed_at: datetime = Field(default_factory=utc_now)
    transport_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("changed_at")
    @classmethod
    def _utc_changed_at(cls, value: datetime) -> datetime:
        return require_utc(value)


class SelfTestCheck(DomainModel):
    name: str
    passed: bool
    message: str | None = None


class SelfTestResult(DomainModel):
    self_test_id: UUID = Field(default_factory=new_uuid)
    sensor_id: str
    status: SelfTestStatus
    started_at: datetime
    completed_at: datetime
    checks: tuple[SelfTestCheck, ...]
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("started_at", "completed_at")
    @classmethod
    def _utc_timestamp(cls, value: datetime) -> datetime:
        return require_utc(value)

    @model_validator(mode="after")
    def _consistent_result(self) -> SelfTestResult:
        if self.completed_at < self.started_at:
            raise ValueError("self-test completion cannot precede its start")
        all_passed = bool(self.checks) and all(check.passed for check in self.checks)
        if (self.status is SelfTestStatus.PASSED) != all_passed:
            raise ValueError("self-test status must agree with its checks")
        return self


class WavelengthCapabilities(DomainModel):
    """Optional optical capability; non-optical sensors omit this object."""

    range: QuantityRange | None = None
    discrete_wavelengths: tuple[QuantityValue, ...] = ()

    @model_validator(mode="after")
    def _defines_support(self) -> WavelengthCapabilities:
        if self.range is None and not self.discrete_wavelengths:
            raise ValueError("wavelength capability must define a range or discrete values")
        for wavelength in self.discrete_wavelengths:
            wavelength.to("nanometer")
        if len({str(item.to("nanometer").value) for item in self.discrete_wavelengths}) != len(
            self.discrete_wavelengths
        ):
            raise ValueError("discrete wavelengths must be unique")
        if self.range is not None:
            self.range.minimum.to("nanometer")
            self.range.maximum.to("nanometer")
        return self

    def supports(self, wavelength: QuantityValue) -> bool:
        requested = wavelength.to("nanometer")
        if self.discrete_wavelengths:
            supported = {
                item.to("nanometer").value for item in self.discrete_wavelengths
            }
            if requested.value in supported:
                return True
        if self.range is None:
            return False
        minimum = self.range.minimum.to("nanometer")
        maximum = self.range.maximum.to("nanometer")
        return minimum.value <= requested.value <= maximum.value


class SensorCapabilities(DomainModel):
    """A point-in-time, hashable capability and calibration snapshot."""

    schema_version: Literal["1"] = "1"
    sensor_id: str
    sensor_type: str
    adapter_name: str
    adapter_version: str
    firmware_version: str | None = None
    measurement_types: frozenset[str]
    measurement_ranges: dict[str, QuantityRange]
    resolutions: dict[str, QuantityValue] = Field(default_factory=dict)
    precisions: dict[str, QuantityValue] = Field(default_factory=dict)
    wavelength: WavelengthCapabilities | None = None
    calibration_required: bool = True
    calibrations: tuple[CalibrationReference, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent_measurements(self) -> SensorCapabilities:
        if not self.measurement_types:
            raise ValueError("sensor must support at least one measurement type")
        for label, mapping in (
            ("measurement range", self.measurement_ranges),
            ("resolution", self.resolutions),
            ("precision", self.precisions),
        ):
            unknown = set(mapping) - set(self.measurement_types)
            if unknown:
                raise ValueError(f"{label} defined for unsupported measurements: {sorted(unknown)}")
        if set(self.measurement_ranges) != set(self.measurement_types):
            raise ValueError("every measurement type must have a measurement range")
        calibration_types = [item.measurement_type for item in self.calibrations]
        if len(calibration_types) != len(set(calibration_types)):
            raise ValueError("capability snapshot has duplicate calibrations")
        if set(calibration_types) - set(self.measurement_types):
            raise ValueError("calibration references an unsupported measurement type")
        return self

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


class MeasurementRequest(DomainModel):
    schema_version: Literal["1"] = "1"
    request_id: UUID = Field(default_factory=new_uuid)
    experiment_id: UUID
    run_id: UUID
    sample_id: str = Field(min_length=1)
    measurement_type: str = Field(min_length=1)
    expected_unit: str
    wavelength: QuantityValue | None = None
    timeout_seconds: FiniteDecimal = Field(default=Decimal(30), gt=0)
    requested_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("requested_at")
    @classmethod
    def _utc_requested_at(cls, value: datetime) -> datetime:
        return require_utc(value)

    @field_validator("expected_unit")
    @classmethod
    def _valid_expected_unit(cls, value: str) -> str:
        return validate_unit(value)

    @field_validator("wavelength")
    @classmethod
    def _wavelength_is_length(cls, value: QuantityValue | None) -> QuantityValue | None:
        if value is not None:
            value.to("nanometer")
        return value


class SensorSourceEvidence(DomainModel):
    """Raw provenance proving a reading crossed a physical sensor boundary."""

    schema_version: Literal["1"] = "1"
    source_type: Literal[MeasurementSourceType.PHYSICAL_SENSOR] = (
        MeasurementSourceType.PHYSICAL_SENSOR
    )
    sensor_id: str
    connection_session_id: UUID
    self_test_id: UUID
    adapter_name: str
    adapter_version: str
    firmware_version: str | None = None
    capability_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    calibration_id: UUID | None = None
    calibration_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    is_mock: bool = False
    acquired_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("acquired_at")
    @classmethod
    def _utc_acquired_at(cls, value: datetime) -> datetime:
        return require_utc(value)

    @model_validator(mode="after")
    def _calibration_pair(self) -> SensorSourceEvidence:
        if (self.calibration_id is None) != (self.calibration_sha256 is None):
            raise ValueError("calibration ID and hash must be supplied together")
        return self


class RawSensorMeasurement(DomainModel):
    """Unprocessed sensor reading suitable for the measurement-ingestion seam."""

    schema_version: Literal["1"] = "1"
    raw_measurement_id: UUID = Field(default_factory=new_uuid)
    request_id: UUID
    experiment_id: UUID
    run_id: UUID
    sample_id: str
    measurement_type: str
    raw_value: FiniteDecimal
    unit: str
    wavelength: QuantityValue | None = None
    measured_at: datetime
    source_evidence: SensorSourceEvidence
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("measured_at")
    @classmethod
    def _utc_measured_at(cls, value: datetime) -> datetime:
        return require_utc(value)

    @field_validator("unit")
    @classmethod
    def _valid_unit(cls, value: str) -> str:
        return validate_unit(value)


@runtime_checkable
class ReadOnlySensor(Protocol):
    """Small asynchronous boundary that a future vendor adapter must implement."""

    @property
    def sensor_id(self) -> str: ...

    @property
    def is_open(self) -> bool: ...

    async def open(self) -> SensorConnection: ...

    async def close(self) -> SensorConnection: ...

    async def capability_manifest(self) -> SensorCapabilities: ...

    async def self_test(self) -> SelfTestResult: ...

    async def read(self, request: MeasurementRequest) -> RawSensorMeasurement: ...
