"""Scientific objective and performance-constraint schemas."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from autonomous_lab.domain.base import DomainModel
from autonomous_lab.domain.enums import ConstraintOperator, ObjectiveDirection
from autonomous_lab.domain.identifiers import new_uuid
from autonomous_lab.domain.quantities import FiniteDecimal, validate_unit


class ObjectiveMetric(DomainModel):
    name: str
    direction: ObjectiveDirection
    unit: str

    @field_validator("name")
    @classmethod
    def _nonempty_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("metric name must not be empty")
        return value

    @field_validator("unit")
    @classmethod
    def _valid_unit(cls, value: str) -> str:
        return validate_unit(value)


class MetricConstraint(DomainModel):
    metric: str
    operator: ConstraintOperator
    threshold: FiniteDecimal
    unit: str = "dimensionless"

    @field_validator("metric")
    @classmethod
    def _nonempty_metric(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("constraint metric must not be empty")
        return value

    @field_validator("unit")
    @classmethod
    def _valid_unit(cls, value: str) -> str:
        return validate_unit(value)


class ScientificObjective(DomainModel):
    schema_version: Literal["1"] = "1"
    objective_id: UUID = Field(default_factory=new_uuid)
    name: str = "assay optimisation"
    metrics: tuple[ObjectiveMetric, ...]
    performance_constraints: tuple[MetricConstraint, ...] = ()
    success_criteria: tuple[MetricConstraint, ...] = ()
    rationale: str | None = None

    @model_validator(mode="after")
    def _unique_metric_names(self) -> ScientificObjective:
        names = [metric.name for metric in self.metrics]
        if not names:
            raise ValueError("at least one objective metric is required")
        if len(names) != len(set(names)):
            raise ValueError("objective metric names must be unique")
        constraint_names = [constraint.metric for constraint in self.performance_constraints]
        if len(constraint_names) != len(set(constraint_names)):
            raise ValueError("performance constraint metric names must be unique")
        return self
