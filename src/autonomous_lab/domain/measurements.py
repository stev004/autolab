"""Immutable raw measurement and derived-analysis artifacts."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, field_validator

from autonomous_lab.domain.base import DomainModel
from autonomous_lab.domain.enums import MeasurementRole
from autonomous_lab.domain.identifiers import new_uuid, require_utc, utc_now
from autonomous_lab.domain.quantities import FiniteDecimal, validate_unit


class Measurement(DomainModel):
    """One raw instrument observation.

    ``raw_value`` is never replaced with a corrected or analysed value. Any such
    calculation is represented by ``DerivedMetric`` with explicit source IDs.
    """

    schema_version: Literal["1"] = "1"
    measurement_id: UUID = Field(default_factory=new_uuid)
    experiment_id: UUID
    run_id: UUID
    step_id: str
    device_id: str
    vessel_id: str
    role: MeasurementRole
    analyte_level: FiniteDecimal | None = None
    replicate_index: int = Field(ge=0)
    measurement_name: str = "absorbance"
    raw_value: FiniteDecimal
    unit: str = "dimensionless"
    measured_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("unit")
    @classmethod
    def _valid_unit(cls, value: str) -> str:
        return validate_unit(value)

    @field_validator("measured_at")
    @classmethod
    def _utc_measured_at(cls, value: datetime) -> datetime:
        return require_utc(value)


class DerivedMetric(DomainModel):
    schema_version: Literal["1"] = "1"
    metric_id: UUID = Field(default_factory=new_uuid)
    experiment_id: UUID
    name: str
    value: FiniteDecimal
    unit: str = "dimensionless"
    source_measurement_ids: tuple[UUID, ...]
    algorithm_name: str
    algorithm_version: str
    uncertainty: FiniteDecimal | None = None
    computed_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("unit")
    @classmethod
    def _valid_unit(cls, value: str) -> str:
        return validate_unit(value)

    @field_validator("computed_at")
    @classmethod
    def _utc_computed_at(cls, value: datetime) -> datetime:
        return require_utc(value)

