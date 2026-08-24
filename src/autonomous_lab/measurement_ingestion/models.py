"""Strict schemas for expected and externally supplied measurements.

These types deliberately do not know about a simulator or a hardware adapter.
They describe the evidence boundary shared by manual entry, file import and
future read-only instruments.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from autonomous_lab.domain.base import DomainModel
from autonomous_lab.domain.enums import MeasurementRole, MeasurementSourceType
from autonomous_lab.domain.identifiers import new_uuid, require_utc, utc_now
from autonomous_lab.domain.quantities import FiniteDecimal, QuantityValue, validate_unit
from autonomous_lab.provenance.hashing import canonical_sha256


class MeasurementAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class MeasurementBatchKind(StrEnum):
    INITIAL = "initial"
    CORRECTION = "correction"


class ImportIssueSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class ExpectedMeasurement(DomainModel):
    """One independently reconcilable item in a measurement plan."""

    expected_measurement_id: str = Field(min_length=1, max_length=255)
    sample_id: str = Field(min_length=1, max_length=128)
    vessel_id: str = Field(min_length=1, max_length=128)
    step_id: str = Field(min_length=1, max_length=255)
    sample_role: MeasurementRole
    measurement_type: str = Field(min_length=1, max_length=128)
    unit: str
    wavelength: QuantityValue | None = None
    analyte_level: FiniteDecimal | None = None
    replicate_index: int = Field(ge=0)
    required: bool = True
    minimum_value: FiniteDecimal | None = None
    maximum_value: FiniteDecimal | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "expected_measurement_id", "sample_id", "vessel_id", "step_id", "measurement_type"
    )
    @classmethod
    def _nonblank_identifier(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("measurement identifiers must not be blank")
        return value

    @field_validator("unit")
    @classmethod
    def _valid_unit(cls, value: str) -> str:
        return validate_unit(value)

    @field_validator("wavelength")
    @classmethod
    def _wavelength_is_length(cls, value: QuantityValue | None) -> QuantityValue | None:
        if value is not None:
            value.to("nanometer")
        return value

    @model_validator(mode="after")
    def _ordered_range(self) -> ExpectedMeasurement:
        if (
            self.minimum_value is not None
            and self.maximum_value is not None
            and self.minimum_value > self.maximum_value
        ):
            raise ValueError("minimum_value cannot exceed maximum_value")
        return self


class ExpectedMeasurementPlan(DomainModel):
    schema_version: Literal["1"] = "1"
    plan_id: UUID = Field(default_factory=new_uuid)
    experiment_id: UUID
    protocol_id: UUID
    protocol_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    entries: tuple[ExpectedMeasurement, ...]
    created_at: datetime = Field(default_factory=utc_now)
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("created_at")
    @classmethod
    def _utc_created_at(cls, value: datetime) -> datetime:
        return require_utc(value)

    @model_validator(mode="after")
    def _unique_entries_and_valid_hash(self) -> ExpectedMeasurementPlan:
        if not self.entries:
            raise ValueError("measurement plan must contain at least one entry")
        identifiers = [item.expected_measurement_id for item in self.entries]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("expected measurement IDs must be unique")
        keys = [measurement_key(item) for item in self.entries]
        if len(keys) != len(set(keys)):
            raise ValueError("sample/type/wavelength measurement keys must be unique")
        expected_hash = canonical_sha256(self, exclude={"plan_sha256"})
        if self.plan_sha256 != expected_hash:
            raise ValueError("plan_sha256 does not match the expected measurement plan")
        return self


class RawMeasurement(DomainModel):
    """One submitted value or explicit statement that a value is unavailable."""

    expected_measurement_id: str = Field(min_length=1, max_length=255)
    sample_id: str = Field(min_length=1, max_length=128)
    measurement_type: str = Field(min_length=1, max_length=128)
    wavelength: QuantityValue | None = None
    availability: MeasurementAvailability = MeasurementAvailability.AVAILABLE
    value: FiniteDecimal | None
    unit: str
    measured_at: datetime
    note: str | None = Field(default=None, max_length=4000)
    unavailable_reason: str | None = Field(default=None, max_length=1000)
    correction_reason: str | None = Field(default=None, max_length=1000)
    source_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("expected_measurement_id", "sample_id", "measurement_type")
    @classmethod
    def _nonblank_identifier(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("measurement identifiers must not be blank")
        return value

    @field_validator("unit")
    @classmethod
    def _valid_unit(cls, value: str) -> str:
        return validate_unit(value)

    @field_validator("measured_at")
    @classmethod
    def _utc_measured_at(cls, value: datetime) -> datetime:
        return require_utc(value)

    @field_validator("wavelength")
    @classmethod
    def _wavelength_is_length(cls, value: QuantityValue | None) -> QuantityValue | None:
        if value is not None:
            value.to("nanometer")
        return value

    @model_validator(mode="after")
    def _availability_matches_value(self) -> RawMeasurement:
        if self.availability is MeasurementAvailability.AVAILABLE:
            if self.value is None:
                raise ValueError("an available measurement requires a value")
            if self.unavailable_reason is not None:
                raise ValueError("an available measurement cannot have unavailable_reason")
        else:
            if self.value is not None:
                raise ValueError("an unavailable measurement cannot have a value")
            if not self.unavailable_reason or not self.unavailable_reason.strip():
                raise ValueError("an unavailable measurement requires a reason")
        return self


class RawMeasurementBatch(DomainModel):
    schema_version: Literal["1"] = "1"
    batch_id: UUID = Field(default_factory=new_uuid)
    experiment_id: UUID
    plan_id: UUID
    source_type: MeasurementSourceType
    batch_kind: MeasurementBatchKind = MeasurementBatchKind.INITIAL
    measurements: tuple[RawMeasurement, ...]
    submitted_by: str = Field(min_length=1, max_length=128)
    submitted_at: datetime = Field(default_factory=utc_now)
    source_file_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_filename: str | None = Field(default=None, max_length=512)
    notes: str | None = Field(default=None, max_length=4000)
    batch_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("submitted_by")
    @classmethod
    def _nonblank_submitter(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("submitted_by must not be blank")
        return value

    @field_validator("source_filename")
    @classmethod
    def _nonblank_filename(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("source_filename must not be blank")
        return value

    @field_validator("submitted_at")
    @classmethod
    def _utc_submitted_at(cls, value: datetime) -> datetime:
        return require_utc(value)

    @model_validator(mode="after")
    def _source_and_hash_consistency(self) -> RawMeasurementBatch:
        identifiers = [item.expected_measurement_id for item in self.measurements]
        if not identifiers:
            raise ValueError("measurement batch must contain at least one measurement")
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("a batch cannot contain duplicate expected measurements")
        is_file = self.source_type is MeasurementSourceType.FILE_IMPORT
        if is_file != (self.source_file_sha256 is not None):
            raise ValueError("file imports require a source file hash and other sources forbid it")
        if is_file != (self.source_filename is not None):
            raise ValueError("file imports require a source filename and other sources forbid it")
        if self.batch_kind is MeasurementBatchKind.CORRECTION:
            if any(not row.correction_reason for row in self.measurements):
                raise ValueError("every corrected measurement requires a correction reason")
        elif any(row.correction_reason is not None for row in self.measurements):
            raise ValueError("initial measurements cannot have a correction reason")
        expected_hash = canonical_sha256(self, exclude={"batch_sha256"})
        if self.batch_sha256 != expected_hash:
            raise ValueError("batch_sha256 does not match the raw measurement batch")
        return self

    @property
    def has_unavailable_values(self) -> bool:
        return any(
            item.availability is MeasurementAvailability.UNAVAILABLE for item in self.measurements
        )


class ImportIssue(DomainModel):
    code: str = Field(min_length=1, max_length=128)
    severity: ImportIssueSeverity = ImportIssueSeverity.ERROR
    message: str = Field(min_length=1)
    row_number: int | None = Field(default=None, ge=1)
    field: str | None = None
    sample_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class MeasurementImportResult(DomainModel):
    plan_id: UUID
    source_file_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    batch: RawMeasurementBatch | None = None
    issues: tuple[ImportIssue, ...] = ()

    @model_validator(mode="after")
    def _all_or_nothing(self) -> MeasurementImportResult:
        has_errors = any(issue.severity is ImportIssueSeverity.ERROR for issue in self.issues)
        if has_errors == (self.batch is not None):
            raise ValueError("an import with errors cannot expose a batch")
        if self.batch is not None and self.batch.source_file_sha256 != self.source_file_sha256:
            raise ValueError("result source file hash must match its accepted batch")
        return self

    @property
    def accepted(self) -> bool:
        return self.batch is not None


def measurement_key(item: ExpectedMeasurement | RawMeasurement) -> tuple[str, str, Decimal | None]:
    wavelength = item.wavelength
    canonical_wavelength = None if wavelength is None else wavelength.to("nanometer").value
    return (item.sample_id, item.measurement_type, canonical_wavelength)


def make_expected_plan(
    *,
    experiment_id: UUID,
    protocol_id: UUID,
    protocol_hash: str,
    entries: tuple[ExpectedMeasurement, ...],
    plan_id: UUID | None = None,
    created_at: datetime | None = None,
) -> ExpectedMeasurementPlan:
    """Create a plan with its hash bound to all scientific inputs."""

    values: dict[str, Any] = {
        "schema_version": "1",
        "plan_id": plan_id or new_uuid(),
        "experiment_id": experiment_id,
        "protocol_id": protocol_id,
        "protocol_hash": protocol_hash,
        "entries": tuple(item.model_dump(mode="python") for item in entries),
        "created_at": created_at or utc_now(),
    }
    return ExpectedMeasurementPlan(**values, plan_sha256=canonical_sha256(values))


def make_raw_batch(
    *,
    experiment_id: UUID,
    plan_id: UUID,
    source_type: MeasurementSourceType,
    measurements: tuple[RawMeasurement, ...],
    submitted_by: str,
    batch_kind: MeasurementBatchKind = MeasurementBatchKind.INITIAL,
    batch_id: UUID | None = None,
    submitted_at: datetime | None = None,
    source_file_sha256: str | None = None,
    source_filename: str | None = None,
    notes: str | None = None,
) -> RawMeasurementBatch:
    values: dict[str, Any] = {
        "schema_version": "1",
        "batch_id": batch_id or new_uuid(),
        "experiment_id": experiment_id,
        "plan_id": plan_id,
        "source_type": source_type,
        "batch_kind": batch_kind,
        "measurements": tuple(item.model_dump(mode="python") for item in measurements),
        "submitted_by": submitted_by,
        "submitted_at": submitted_at or utc_now(),
        "source_file_sha256": source_file_sha256,
        "source_filename": source_filename,
        "notes": notes,
    }
    return RawMeasurementBatch(**values, batch_sha256=canonical_sha256(values))
