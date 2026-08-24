"""Immutable calibration artifacts and deterministic validity checks."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from autonomous_lab.domain.base import DomainModel
from autonomous_lab.domain.identifiers import new_uuid, require_utc, utc_now
from autonomous_lab.domain.quantities import FiniteDecimal, QuantityValue
from autonomous_lab.provenance.hashing import canonical_sha256


class CalibrationStatus(StrEnum):
    VALID = "valid"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"
    SUPERSEDED = "superseded"


class CalibrationRecord(DomainModel):
    """One immutable sensor calibration and its traceable source material."""

    schema_version: Literal["1"] = "1"
    calibration_id: UUID = Field(default_factory=new_uuid)
    sensor_id: str = Field(min_length=1)
    measurement_type: str = Field(min_length=1)
    calibrated_at: datetime
    expires_at: datetime | None = None
    method: str = Field(min_length=1)
    reference_material: str = Field(min_length=1)
    coefficients: dict[str, FiniteDecimal] = Field(default_factory=dict)
    parameters: dict[str, QuantityValue] = Field(default_factory=dict)
    raw_artifact_id: str | None = None
    raw_artifact_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    status: CalibrationStatus = CalibrationStatus.VALID
    supersedes_calibration_id: UUID | None = None
    created_at: datetime = Field(default_factory=utc_now)
    notes: str | None = None

    @field_validator("calibrated_at", "expires_at", "created_at")
    @classmethod
    def _utc_timestamp(cls, value: datetime | None) -> datetime | None:
        return None if value is None else require_utc(value)

    @model_validator(mode="after")
    def _consistent_lifecycle(self) -> CalibrationRecord:
        if self.expires_at is not None and self.expires_at <= self.calibrated_at:
            raise ValueError("calibration expiry must follow calibration timestamp")
        if (self.raw_artifact_id is None) != (self.raw_artifact_sha256 is None):
            raise ValueError("raw calibration artifact ID and SHA-256 must be supplied together")
        if self.supersedes_calibration_id == self.calibration_id:
            raise ValueError("calibration cannot supersede itself")
        return self

    @property
    def sha256(self) -> str:
        """Hash the complete immutable record, including its lifecycle status."""

        return canonical_sha256(self)

    def effective_status(self, at: datetime) -> CalibrationStatus:
        """Return the deterministic lifecycle status at ``at`` without mutation."""

        require_utc(at)
        if self.status is not CalibrationStatus.VALID:
            return self.status
        if self.expires_at is not None and at >= self.expires_at:
            return CalibrationStatus.EXPIRED
        return CalibrationStatus.VALID


class CalibrationReference(DomainModel):
    """Calibration facts embedded in a point-in-time capability manifest."""

    calibration_id: UUID
    calibration_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    measurement_type: str
    status: CalibrationStatus
    calibrated_at: datetime
    expires_at: datetime | None = None

    @field_validator("calibrated_at", "expires_at")
    @classmethod
    def _utc_timestamp(cls, value: datetime | None) -> datetime | None:
        return None if value is None else require_utc(value)

    @classmethod
    def from_record(cls, record: CalibrationRecord, *, at: datetime) -> CalibrationReference:
        require_utc(at)
        return cls(
            calibration_id=record.calibration_id,
            calibration_sha256=record.sha256,
            measurement_type=record.measurement_type,
            status=record.effective_status(at),
            calibrated_at=record.calibrated_at,
            expires_at=record.expires_at,
        )


class CalibrationIssue(DomainModel):
    code: str
    message: str


class CalibrationValidationResult(DomainModel):
    valid: bool
    effective_status: CalibrationStatus | None
    calibration_id: UUID | None
    calibration_sha256: str | None
    issues: tuple[CalibrationIssue, ...] = ()


def validate_calibration(
    record: CalibrationRecord | None,
    *,
    sensor_id: str,
    measurement_type: str,
    at: datetime,
) -> CalibrationValidationResult:
    """Validate identity, measurement type, and lifecycle before physical reads."""

    require_utc(at)
    if record is None:
        return CalibrationValidationResult(
            valid=False,
            effective_status=None,
            calibration_id=None,
            calibration_sha256=None,
            issues=(
                CalibrationIssue(
                    code="calibration_missing",
                    message=f"sensor {sensor_id!r} has no calibration",
                ),
            ),
        )

    issues: list[CalibrationIssue] = []
    if record.sensor_id != sensor_id:
        issues.append(
            CalibrationIssue(
                code="calibration_sensor_mismatch",
                message=(
                    f"calibration belongs to {record.sensor_id!r}, not sensor {sensor_id!r}"
                ),
            )
        )
    if record.measurement_type != measurement_type:
        issues.append(
            CalibrationIssue(
                code="calibration_measurement_mismatch",
                message=(
                    f"calibration is for {record.measurement_type!r}, "
                    f"not {measurement_type!r}"
                ),
            )
        )
    status = record.effective_status(at)
    if status is not CalibrationStatus.VALID:
        issues.append(
            CalibrationIssue(
                code=f"calibration_{status.value}",
                message=f"calibration is {status.value}",
            )
        )
    return CalibrationValidationResult(
        valid=not issues,
        effective_status=status,
        calibration_id=record.calibration_id,
        calibration_sha256=record.sha256,
        issues=tuple(issues),
    )
