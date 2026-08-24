"""Scientific experiment proposal contracts."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator

from autonomous_lab.domain.base import DomainModel
from autonomous_lab.domain.identifiers import new_uuid, require_utc, utc_now
from autonomous_lab.domain.quantities import FiniteDecimal, QuantityValue


class ExperimentProposal(DomainModel):
    """Scientific conditions suggested by an optimiser, never device commands."""

    schema_version: Literal["1"] = "1"
    proposal_id: UUID = Field(default_factory=new_uuid)
    experiment_id: UUID = Field(default_factory=new_uuid)
    campaign_id: UUID
    suggestion_index: int = Field(ge=0)
    parameter_values: dict[str, QuantityValue]
    strategy_name: str
    strategy_version: str
    strategy_trial_id: str
    parent_experiment_ids: tuple[UUID, ...] = ()
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at")
    @classmethod
    def _utc_created_at(cls, value: datetime) -> datetime:
        return require_utc(value)

    @field_validator("parameter_values")
    @classmethod
    def _has_parameters(cls, value: dict[str, QuantityValue]) -> dict[str, QuantityValue]:
        if not value:
            raise ValueError("an experiment proposal must contain parameter values")
        return value


class PerformanceConstraintResult(DomainModel):
    """Scientific feasibility result; this is not a run-integrity QC check."""

    metric: str
    residual: FiniteDecimal
    passed: bool


class ExperimentResult(DomainModel):
    """Links immutable raw, QC, and derived artifacts for one execution run."""

    schema_version: Literal["1"] = "1"
    result_id: UUID = Field(default_factory=new_uuid)
    experiment_id: UUID
    run_id: UUID
    measurement_ids: tuple[UUID, ...]
    qc_result_id: UUID | None = None
    derived_metric_ids: tuple[UUID, ...] = ()
    performance_constraints: tuple[PerformanceConstraintResult, ...] = ()
    optimizer_eligible: bool = False
    completed_at: datetime = Field(default_factory=utc_now)

    @field_validator("completed_at")
    @classmethod
    def _utc_completed_at(cls, value: datetime) -> datetime:
        return require_utc(value)
