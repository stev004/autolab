"""Campaign document for culture experiments (``experiment_type: culture``).

Kept separate from the assay ``CampaignConfig`` because the two share almost no
scientific design fields: an assay needs plate layout, standards and a simulator
model; a culture needs a reactor, a hold duration and a sampling schedule. What
they do share (campaign metadata, objectives, parameter space, optimiser) is
reused unchanged.

The trusted ``CulturePolicy`` lives in its own installation file and cannot be
overridden by this document.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from autonomous_lab.campaigns.config import (
    CampaignMetadata,
    ObjectiveConfig,
    OptimizationConfig,
    SensorConfig,
    validate_grid_budget,
)
from autonomous_lab.culture.models import (
    CULTURE_PARAMETERS,
    LIGHT_PARAMETER,
    LIGHT_UNIT,
    TEMPERATURE_PARAMETER,
    TEMPERATURE_UNIT,
    CultureDesign,
    CultureQCConfig,
)
from autonomous_lab.domain.base import DomainModel
from autonomous_lab.domain.parameters import ParameterSpace
from autonomous_lab.domain.quantities import QuantityValue

#: Metrics the culture analysis actually produces. Objectives must come from here.
CULTURE_METRICS = frozenset({"final_od750", "energy_kwh"})


class ControllerConfig(DomainModel):
    """Which installed controller adapter drives the reactor, and its own settings."""

    adapter: str
    controller_id: str
    settings: dict[str, object] = Field(default_factory=dict)

    @field_validator("adapter", "controller_id")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("controller configuration values cannot be blank")
        return value.strip()


class CultureExecutionConfig(DomainModel):
    controller: ControllerConfig
    #: Omit when the controller has an integrated OD probe (it then supplies the sensor).
    sensor: SensorConfig | None = None
    #: ``simulated`` skips real waiting and is refused unless the controller is simulated.
    clock: Literal["wall", "simulated"] = "wall"

    @model_validator(mode="after")
    def _no_plate_fixture(self) -> CultureExecutionConfig:
        if self.sensor is not None and self.sensor.adapter == "mock_physical":
            raise ValueError("the plate-fixture mock sensor cannot sample a culture time series")
        return self


class CultureCampaignConfig(DomainModel):
    schema_version: Literal["1"]
    experiment_type: Literal["culture"]
    campaign: CampaignMetadata
    objective: ObjectiveConfig
    parameter_space: ParameterSpace
    culture_design: CultureDesign
    culture_qc: CultureQCConfig = CultureQCConfig()
    execution: CultureExecutionConfig
    optimization: OptimizationConfig

    @model_validator(mode="after")
    def _culture_semantics(self) -> CultureCampaignConfig:
        if self.optimization.startup_experiments > self.campaign.max_experiments:
            raise ValueError("startup_experiments cannot exceed campaign max_experiments")
        names = frozenset(self.parameter_space.parameters)
        if names != CULTURE_PARAMETERS:
            raise ValueError(
                f"culture campaigns optimise exactly {sorted(CULTURE_PARAMETERS)}; "
                f"got {sorted(names)}"
            )
        for name, unit in (
            (TEMPERATURE_PARAMETER, TEMPERATURE_UNIT),
            (LIGHT_PARAMETER, LIGHT_UNIT),
        ):
            parameter = self.parameter_space.parameters[name]
            # Fails for incompatible units (e.g. light given in degC).
            QuantityValue(value=parameter.minimum, unit=parameter.unit).to(unit)
        used = {metric.name for metric in self.objective.metrics} | {
            item.metric
            for item in (*self.objective.performance_constraints, *self.objective.success_criteria)
        }
        unknown = used - CULTURE_METRICS
        if unknown:
            raise ValueError(
                f"culture analysis does not produce {sorted(unknown)}; "
                f"available metrics: {sorted(CULTURE_METRICS)}"
            )
        validate_grid_budget(self.parameter_space, self.optimization, self.campaign.max_experiments)
        return self
