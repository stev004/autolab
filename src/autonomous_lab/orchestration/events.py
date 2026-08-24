"""Immutable execution results returned by the orchestrator."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, field_validator

from autonomous_lab.domain.base import DomainModel
from autonomous_lab.domain.identifiers import require_utc
from autonomous_lab.domain.measurements import Measurement
from autonomous_lab.hardware.commands import DeviceResponse, ExecutionEvidence


class ActionAttempt(DomainModel):
    event_id: UUID
    sequence: int = Field(ge=0)
    step_id: str
    idempotency_key: str
    attempt: int = Field(ge=1)
    retry_safe: bool
    device_id: str
    status: Literal["succeeded", "failed"]
    started_at: datetime
    completed_at: datetime
    response: DeviceResponse | None = None
    error_category: str | None = None
    error_message: str | None = None
    ambiguous: bool = False

    @field_validator("started_at", "completed_at")
    @classmethod
    def _utc_timestamps(cls, value: datetime) -> datetime:
        return require_utc(value)


class ExecutionRecord(DomainModel):
    run_id: UUID
    experiment_id: UUID
    protocol_id: UUID
    validation_report_id: UUID
    status: Literal["succeeded", "failed"]
    started_at: datetime
    completed_at: datetime
    actions: tuple[ActionAttempt, ...]
    measurements: tuple[Measurement, ...]
    execution_evidence: ExecutionEvidence | None = None
    failure_category: str | None = None
    failure_message: str | None = None
    safe_state_reached: bool | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("started_at", "completed_at")
    @classmethod
    def _utc_timestamps(cls, value: datetime) -> datetime:
        return require_utc(value)
