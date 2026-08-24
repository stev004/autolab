"""Typed provenance and operator-worklist models for human execution.

These models deliberately describe evidence without pretending that an operator is
a hardware device.  They are storage-agnostic so the same immutable artifacts can
later be persisted by the application layer.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from autonomous_lab.domain.base import DomainModel
from autonomous_lab.domain.enums import ExecutionMode, ExecutorType
from autonomous_lab.domain.identifiers import new_uuid, require_utc, utc_now
from autonomous_lab.domain.protocols import ProtocolCommand
from autonomous_lab.domain.quantities import QuantityValue


class ExecutionStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"
    ABORTED = "aborted"


class ExecutionConfidence(StrEnum):
    CONFIRMED = "confirmed"
    ESTIMATED = "estimated"
    UNVERIFIED = "unverified"


class AcknowledgementDecision(StrEnum):
    CONFIRM = "confirm"
    RECORD_DEVIATION = "record_deviation"
    SKIP = "skip"
    ABORT = "abort"
    MARK_FAILED = "mark_failed"


class DeviationCategory(StrEnum):
    QUANTITY_DEVIATION = "quantity_deviation"
    TIMING_DEVIATION = "timing_deviation"
    MISSING_SAMPLE = "missing_sample"
    CONTAMINATION_SUSPECTED = "contamination_suspected"
    OPERATOR_ERROR = "operator_error"
    DEVICE_ERROR = "device_error"
    OTHER = "other"


class ProtocolDeviation(DomainModel):
    deviation_id: UUID = Field(default_factory=new_uuid)
    category: DeviationCategory
    description: str
    requested_quantity: QuantityValue | None = None
    actual_quantity: QuantityValue | None = None
    material_id: str | None = None
    vessel_id: str | None = None
    recorded_by: str
    recorded_at: datetime = Field(default_factory=utc_now)

    @field_validator("description", "recorded_by")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be empty")
        return value.strip()

    @field_validator("recorded_at")
    @classmethod
    def _utc_recorded_at(cls, value: datetime) -> datetime:
        return require_utc(value)

    @model_validator(mode="after")
    def _compatible_quantities(self) -> ProtocolDeviation:
        if (
            self.requested_quantity is not None
            and self.actual_quantity is not None
            and not self.requested_quantity.is_compatible_with(self.actual_quantity)
        ):
            raise ValueError("requested and actual deviation quantities must be compatible")
        if self.category in {
            DeviationCategory.QUANTITY_DEVIATION,
            DeviationCategory.TIMING_DEVIATION,
        } and (self.requested_quantity is None or self.actual_quantity is None):
            raise ValueError("quantity and timing deviations require requested and actual values")
        return self


class StepAcknowledgement(DomainModel):
    acknowledgement_id: UUID = Field(default_factory=new_uuid)
    group_id: str
    step_ids: tuple[str, ...]
    operator_id: str
    decision: AcknowledgementDecision
    acknowledged_at: datetime = Field(default_factory=utc_now)
    actual_quantities: dict[str, QuantityValue] = Field(default_factory=dict)
    deviations: tuple[ProtocolDeviation, ...] = ()
    note: str | None = None
    evidence_artifact_ids: tuple[str, ...] = ()

    @field_validator("group_id", "operator_id")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be empty")
        return value.strip()

    @field_validator("step_ids")
    @classmethod
    def _has_unique_steps(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("an acknowledgement must cover at least one step")
        if len(value) != len(set(value)):
            raise ValueError("acknowledged step IDs must be unique")
        return value

    @field_validator("acknowledged_at")
    @classmethod
    def _utc_acknowledged_at(cls, value: datetime) -> datetime:
        return require_utc(value)

    @model_validator(mode="after")
    def _decision_has_required_context(self) -> StepAcknowledgement:
        if self.decision is AcknowledgementDecision.RECORD_DEVIATION and not self.deviations:
            raise ValueError("record_deviation acknowledgement requires a deviation")
        if self.decision in {
            AcknowledgementDecision.ABORT,
            AcknowledgementDecision.MARK_FAILED,
        } and (self.note is None or not self.note.strip()):
            raise ValueError("abort and failure acknowledgements require a note")
        return self


class StepExecutionEvidence(DomainModel):
    evidence_id: UUID = Field(default_factory=new_uuid)
    experiment_id: UUID
    protocol_id: UUID
    step_id: str
    group_id: str
    executor_type: ExecutorType
    executor_id: str
    execution_mode: ExecutionMode
    requested_action: ProtocolCommand
    observed_action: str | None = None
    started_at: datetime
    completed_at: datetime | None = None
    status: ExecutionStatus
    confidence: ExecutionConfidence
    actual_quantities: dict[str, QuantityValue] = Field(default_factory=dict)
    deviations: tuple[ProtocolDeviation, ...] = ()
    note: str | None = None
    evidence_artifact_ids: tuple[str, ...] = ()
    acknowledgement_id: UUID | None = None

    @field_validator("started_at", "completed_at")
    @classmethod
    def _utc_timestamps(cls, value: datetime | None) -> datetime | None:
        return None if value is None else require_utc(value)

    @model_validator(mode="after")
    def _consistent_timeline_and_status(self) -> StepExecutionEvidence:
        terminal = {
            ExecutionStatus.COMPLETED,
            ExecutionStatus.SKIPPED,
            ExecutionStatus.FAILED,
            ExecutionStatus.ABORTED,
        }
        if self.status in terminal and self.completed_at is None:
            raise ValueError("terminal execution evidence requires completed_at")
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("completed_at cannot precede started_at")
        if self.status is ExecutionStatus.COMPLETED and self.acknowledgement_id is None:
            raise ValueError("completed human evidence requires an acknowledgement")
        if (
            self.execution_mode in {ExecutionMode.HUMAN_MANUAL, ExecutionMode.HUMAN_SENSOR}
            and self.executor_type is not ExecutorType.HUMAN_OPERATOR
        ):
            raise ValueError("human execution modes require a human operator executor")
        return self


class HumanExecutionRecord(DomainModel):
    schema_version: Literal["1"] = "1"
    record_id: UUID = Field(default_factory=new_uuid)
    experiment_id: UUID
    protocol_id: UUID
    worklist_id: UUID
    operator_id: str
    execution_mode: Literal[ExecutionMode.HUMAN_MANUAL, ExecutionMode.HUMAN_SENSOR]
    status: ExecutionStatus
    started_at: datetime
    completed_at: datetime | None = None
    acknowledgements: tuple[StepAcknowledgement, ...]
    step_evidence: tuple[StepExecutionEvidence, ...]
    deviations: tuple[ProtocolDeviation, ...] = ()
    notes: str | None = None

    @field_validator("started_at", "completed_at")
    @classmethod
    def _utc_timestamps(cls, value: datetime | None) -> datetime | None:
        return None if value is None else require_utc(value)

    @model_validator(mode="after")
    def _complete_and_consistent(self) -> HumanExecutionRecord:
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("completed_at cannot precede started_at")
        if self.status is ExecutionStatus.COMPLETED:
            if self.completed_at is None:
                raise ValueError("completed record requires completed_at")
            if any(item.status is not ExecutionStatus.COMPLETED for item in self.step_evidence):
                raise ValueError("completed record cannot contain incomplete step evidence")
        evidence_steps = [item.step_id for item in self.step_evidence]
        if len(evidence_steps) != len(set(evidence_steps)):
            raise ValueError("step evidence must contain unique step IDs")
        return self
