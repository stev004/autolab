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

import re
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
    KNOWN_VARIABLE_UNITS,
    RESOURCE_METRICS,
    CultureDesign,
    CultureQCConfig,
)
from autonomous_lab.domain.base import DomainModel
from autonomous_lab.domain.parameters import ParameterSpace
from autonomous_lab.domain.quantities import QuantityValue

#: Metrics the culture analysis actually produces. Objectives must come from here.
CULTURE_METRICS = frozenset(
    {"final_od750", *(metric for metric, _unit in RESOURCE_METRICS.values())}
)
_VARIABLE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


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
    #: What a cancelled run (Ctrl-C, shutdown) does. ``safe_state`` ends the hold;
    #: ``keep_holding`` leaves the device under its own local control so the next
    #: ``culture run`` resumes the culture where it left off.
    on_interrupt: Literal["safe_state", "keep_holding"] = "safe_state"

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
        # Parameters ARE controlled-variable names; the device must declare each one
        # (checked when the campaign runs, against the live manifest).
        optimised = set(self.parameter_space.parameters)
        fixed = set(self.culture_design.fixed_setpoints)
        if not optimised:
            raise ValueError("a culture campaign must optimise at least one variable")
        if optimised & fixed:
            raise ValueError(f"variables are both optimised and fixed: {sorted(optimised & fixed)}")
        for name in optimised | fixed:
            if not _VARIABLE_NAME.match(name):
                raise ValueError(f"{name!r} is not a valid controlled-variable name")
            unit = KNOWN_VARIABLE_UNITS.get(name)
            if unit is None:
                continue
            # Fails for incompatible units (e.g. light given in degC).
            if name in optimised:
                parameter = self.parameter_space.parameters[name]
                QuantityValue(value=parameter.minimum, unit=parameter.unit).to(unit)
            else:
                self.culture_design.fixed_setpoints[name].to(unit)
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
