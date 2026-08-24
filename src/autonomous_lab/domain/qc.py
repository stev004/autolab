"""Run-integrity quality-control artifacts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from autonomous_lab.domain.base import DomainModel
from autonomous_lab.domain.enums import QCStatus
from autonomous_lab.domain.identifiers import new_uuid, require_utc, utc_now


class QCCheckResult(DomainModel):
    check_name: str
    check_version: str
    passed: bool
    message: str
    flagged_measurement_ids: tuple[UUID, ...] = ()
    details: dict[str, Any] = Field(default_factory=dict)


class OutlierAssessment(DomainModel):
    method: str
    threshold: str
    flagged_measurement_ids: tuple[UUID, ...] = ()
    metrics_recomputed_with_exclusions: bool = False


class QCResult(DomainModel):
    schema_version: Literal["1"] = "1"
    qc_result_id: UUID = Field(default_factory=new_uuid)
    experiment_id: UUID
    run_id: UUID
    status: QCStatus
    checks: tuple[QCCheckResult, ...]
    outlier_assessment: OutlierAssessment | None = None
    evaluated_at: datetime = Field(default_factory=utc_now)

    @field_validator("evaluated_at")
    @classmethod
    def _utc_evaluated_at(cls, value: datetime) -> datetime:
        return require_utc(value)

    @model_validator(mode="after")
    def _consistent_status(self) -> QCResult:
        all_passed = all(check.passed for check in self.checks)
        if self.status is QCStatus.PASSED and not all_passed:
            raise ValueError("passing QC result cannot contain a failed integrity check")
        if self.status is QCStatus.FAILED and all_passed:
            raise ValueError("failed QC result must contain a failed integrity check")
        return self
