"""Optimiser-facing contracts that contain no protocol or device operations."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, field_validator

from autonomous_lab.domain.base import DomainModel
from autonomous_lab.domain.identifiers import new_uuid, require_utc, utc_now
from autonomous_lab.domain.quantities import FiniteDecimal


class OptimizationObservation(DomainModel):
    schema_version: Literal["1"] = "1"
    observation_id: UUID = Field(default_factory=new_uuid)
    campaign_id: UUID
    experiment_id: UUID
    result_id: UUID
    objective_values: dict[str, FiniteDecimal]
    constraint_residuals: dict[str, FiniteDecimal]
    feasible: bool
    qc_eligible: bool
    failure_category: str | None = None
    measurement_uncertainty: dict[str, FiniteDecimal] = Field(default_factory=dict)
    observed_at: datetime = Field(default_factory=utc_now)

    @field_validator("observed_at")
    @classmethod
    def _utc_observed_at(cls, value: datetime) -> datetime:
        return require_utc(value)


class OptimizationState(DomainModel):
    schema_version: Literal["1"] = "1"
    state_id: UUID = Field(default_factory=new_uuid)
    campaign_id: UUID
    strategy_name: str
    strategy_version: str
    seed: int = Field(ge=0)
    suggestion_count: int = Field(default=0, ge=0)
    observation_count: int = Field(default=0, ge=0)
    checkpoint_data: dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("updated_at")
    @classmethod
    def _utc_updated_at(cls, value: datetime) -> datetime:
        return require_utc(value)


class OptimizerCheckpoint(DomainModel):
    schema_version: Literal["1"] = "1"
    checkpoint_id: UUID = Field(default_factory=new_uuid)
    campaign_id: UUID
    state: OptimizationState
    artifact_uri: str | None = None
    artifact_sha256: str | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at")
    @classmethod
    def _utc_created_at(cls, value: datetime) -> datetime:
        return require_utc(value)

