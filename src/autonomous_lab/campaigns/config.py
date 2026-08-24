"""Strict YAML/JSON campaign and trusted validation-policy schemas."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import Field, field_validator, model_validator

from autonomous_lab.domain.base import DomainModel
from autonomous_lab.domain.enums import CommandType, ControlType
from autonomous_lab.domain.metric_registry import metric_definition
from autonomous_lab.domain.objectives import MetricConstraint, ObjectiveMetric
from autonomous_lab.domain.parameters import ParameterSpace
from autonomous_lab.domain.protocols import QuantityRange
from autonomous_lab.domain.quantities import FiniteDecimal, QuantityValue
from autonomous_lab.simulation.model_versions import (
    ASSAY_MAXIMUM_WELL_VOLUME_MICROLITER,
    ASSAY_MODEL_CONFIG_NAME,
    ASSAY_PARAMETER_DOMAIN,
    ASSAY_WAVELENGTH_NANOMETER,
)


class CampaignMetadata(DomainModel):
    name: str
    seed: int = Field(ge=0)
    max_experiments: int = Field(ge=1)

    @field_validator("name")
    @classmethod
    def _nonempty_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("campaign name must not be empty")
        return value


class ObjectiveConfig(DomainModel):
    """ID-free objective definition used in a hash-stable campaign document."""

    metrics: tuple[ObjectiveMetric, ...]
    performance_constraints: tuple[MetricConstraint, ...] = ()
    success_criteria: tuple[MetricConstraint, ...] = ()

    @model_validator(mode="after")
    def _unique_names(self) -> ObjectiveConfig:
        names = [metric.name for metric in self.metrics]
        if not names or len(names) != len(set(names)):
            raise ValueError("objective metric names must be nonempty and unique")
        constrained = [constraint.metric for constraint in self.performance_constraints]
        if len(constrained) != len(set(constrained)):
            raise ValueError("performance constraints must target unique metric names")
        success_names = [constraint.metric for constraint in self.success_criteria]
        if len(success_names) != len(set(success_names)):
            raise ValueError("success criteria must target unique metric names")
        for metric in self.metrics:
            definition = metric_definition(metric.name)
            if metric.unit != definition.canonical_unit:
                raise ValueError(
                    f"objective metric {metric.name!r} must use canonical unit "
                    f"{definition.canonical_unit!r}"
                )
            if metric.direction not in definition.objective_directions:
                raise ValueError(
                    f"metric {metric.name!r} does not support objective direction "
                    f"{metric.direction.value!r}"
                )
        for constraint in (*self.performance_constraints, *self.success_criteria):
            definition = metric_definition(constraint.metric)
            if constraint.unit != definition.canonical_unit:
                raise ValueError(
                    f"constraint metric {constraint.metric!r} must use canonical unit "
                    f"{definition.canonical_unit!r}"
                )
            if constraint.operator not in definition.constraint_operators:
                raise ValueError(
                    f"metric {constraint.metric!r} does not support constraint operator "
                    f"{constraint.operator.value!r}"
                )
        return self


class AssayDesignConfig(DomainModel):
    wavelength: QuantityValue
    blank_replicates: int = Field(ge=1)
    standard_levels: tuple[FiniteDecimal, ...]
    standard_replicates: int = Field(ge=1)
    control_level: FiniteDecimal
    control_replicates: int = Field(ge=1)
    check_standard_level: FiniteDecimal
    check_standard_replicates: int = Field(default=1, ge=1)
    maximum_well_volume: QuantityValue

    @model_validator(mode="after")
    def _scientifically_usable_layout(self) -> AssayDesignConfig:
        self.wavelength.to("nanometer")
        self.maximum_well_volume.to("microliter")
        if len(self.standard_levels) < 3:
            raise ValueError("at least three calibration levels are required")
        if len(self.standard_levels) != len(set(self.standard_levels)):
            raise ValueError("calibration levels must be unique")
        if tuple(sorted(self.standard_levels)) != self.standard_levels:
            raise ValueError("calibration levels must be strictly increasing")
        if self.blank_replicates < 3:
            raise ValueError("V0 requires at least three blank replicates")
        if self.standard_replicates < 3:
            raise ValueError("V0 requires at least three calibration replicates")
        if self.control_replicates < 3:
            raise ValueError("V0 requires at least three control replicates")
        if any(level < 0 or level > 1 for level in self.standard_levels):
            raise ValueError("V0 standard levels must lie within the simulator range [0, 1]")
        if self.standard_levels[0] != 0 or self.standard_levels[-1] != 1:
            raise ValueError("V0 calibration must span the complete normalized range [0, 1]")
        if not self.standard_levels[0] < self.control_level < self.standard_levels[-1]:
            raise ValueError("positive-control level must lie inside the calibration range")
        if not self.standard_levels[0] < self.check_standard_level < self.standard_levels[-1]:
            raise ValueError("check-standard level must lie inside the calibration range")
        if self.check_standard_level in self.standard_levels:
            raise ValueError("check-standard level must be independent of calibration levels")
        if self.check_standard_level == self.control_level:
            raise ValueError("check-standard and positive-control levels must differ")
        well_count = (
            self.blank_replicates
            + len(self.standard_levels) * self.standard_replicates
            + self.control_replicates
            + self.check_standard_replicates
        )
        if well_count > 96:
            raise ValueError("V0 assay batch must fit in one 96-well plate")
        return self


class ExecutionConfig(DomainModel):
    mode: Literal["simulation", "human_manual", "human_sensor"] = "simulation"
    backend: Literal["simulated"] | None = None
    capability_profile: Literal["simulated_assay_v1", "human_assay_v1"]
    measurement_source: Literal[
        "manual_entry", "csv_import", "mock_physical_sensor"
    ] | None = None
    operator_id: str | None = None
    sensor: SensorConfig | None = None

    @model_validator(mode="after")
    def _coherent_execution_boundary(self) -> ExecutionConfig:
        if self.mode == "simulation":
            if self.backend not in {None, "simulated"}:
                raise ValueError("simulation mode requires the simulated backend")
            if self.capability_profile != "simulated_assay_v1":
                raise ValueError("simulation mode requires simulated_assay_v1 capabilities")
            if self.measurement_source is not None:
                raise ValueError("simulation measurements are owned by the simulated backend")
            if self.sensor is not None:
                raise ValueError("simulation mode cannot configure a physical sensor boundary")
        else:
            if self.backend is not None:
                raise ValueError("human execution modes cannot configure a machine backend")
            if self.capability_profile != "human_assay_v1":
                raise ValueError("human modes require human_assay_v1 capabilities")
            if self.mode == "human_manual" and self.measurement_source not in {
                "manual_entry",
                "csv_import",
            }:
                raise ValueError("human_manual requires manual_entry or csv_import")
            if self.mode == "human_manual" and self.sensor is not None:
                raise ValueError("human_manual cannot configure a sensor")
            if (
                self.mode == "human_sensor"
                and self.measurement_source != "mock_physical_sensor"
            ):
                raise ValueError("human_sensor V0.5 requires mock_physical_sensor")
            if self.mode == "human_sensor" and self.sensor is None:
                raise ValueError("human_sensor requires sensor configuration")
        if self.operator_id is not None and not self.operator_id.strip():
            raise ValueError("operator_id cannot be blank")
        return self


class SensorConfig(DomainModel):
    adapter: Literal["mock_physical"]
    sensor_id: str
    fixture_path: str

    @field_validator("sensor_id", "fixture_path")
    @classmethod
    def _nonempty_sensor_value(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("sensor configuration values cannot be blank")
        return value.strip()


class SimulationCostConfig(DomainModel):
    fixed_consumables: QuantityValue = QuantityValue(value=Decimal("0.05"), unit="GBP")
    reagent_per_milligram: QuantityValue = QuantityValue(
        value=Decimal("0.08"), unit="GBP / milligram"
    )
    sample_per_microliter: QuantityValue = QuantityValue(
        value=Decimal("0.0002"), unit="GBP / microliter"
    )
    incubation_per_minute: QuantityValue = QuantityValue(
        value=Decimal("0.0005"), unit="GBP / minute"
    )

    @model_validator(mode="after")
    def _canonical_nonnegative_costs(self) -> SimulationCostConfig:
        expected_units = {
            "fixed_consumables": "GBP",
            "reagent_per_milligram": "GBP / milligram",
            "sample_per_microliter": "GBP / microliter",
            "incubation_per_minute": "GBP / minute",
        }
        for field_name, expected_unit in expected_units.items():
            quantity = getattr(self, field_name)
            converted = quantity.to(expected_unit)
            if quantity.unit != expected_unit:
                raise ValueError(
                    f"simulation cost {field_name!r} must use canonical unit {expected_unit!r}"
                )
            if converted.value < 0:
                raise ValueError(f"simulation cost {field_name!r} cannot be negative")
        return self


class SimulationConfig(DomainModel):
    model: Literal["nonlinear_colorimetric_assay_v1"]
    seed: int = Field(ge=0)
    outlier_probability: FiniteDecimal = Field(ge=0, le=1)
    cost: SimulationCostConfig = SimulationCostConfig()


class OptimizationConfig(DomainModel):
    strategy: Literal["random", "grid", "bayesian"]
    startup_experiments: int = Field(default=12, ge=0)
    maximum_grid_size: int = Field(default=10_000, ge=1)


class CampaignConfig(DomainModel):
    schema_version: Literal["1"]
    campaign: CampaignMetadata
    objective: ObjectiveConfig
    parameter_space: ParameterSpace
    assay_design: AssayDesignConfig
    execution: ExecutionConfig
    simulation: SimulationConfig
    optimization: OptimizationConfig

    @model_validator(mode="after")
    def _v0_semantics(self) -> CampaignConfig:
        if self.optimization.startup_experiments > self.campaign.max_experiments:
            raise ValueError("startup_experiments cannot exceed campaign max_experiments")
        if self.simulation.model != ASSAY_MODEL_CONFIG_NAME:
            raise ValueError(f"unsupported V0 simulator model: {self.simulation.model!r}")
        self._validate_parameter_space()
        self._validate_assay_physical_limits()
        self._validate_grid_budget()
        return self

    def _validate_parameter_space(self) -> None:
        actual = set(self.parameter_space.parameters)
        expected = set(ASSAY_PARAMETER_DOMAIN)
        if actual != expected:
            raise ValueError(
                "V0 requires exactly the simulator parameters; "
                f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
            )
        for name, supported in ASSAY_PARAMETER_DOMAIN.items():
            configured = self.parameter_space.parameters[name]
            minimum = QuantityValue(value=configured.minimum, unit=configured.unit).to(
                supported.canonical_unit
            )
            maximum = QuantityValue(value=configured.maximum, unit=configured.unit).to(
                supported.canonical_unit
            )
            if minimum.value < supported.minimum or maximum.value > supported.maximum:
                raise ValueError(
                    f"parameter {name!r} lies outside simulator domain "
                    f"[{supported.minimum}, {supported.maximum}] {supported.canonical_unit}"
                )

    def _validate_assay_physical_limits(self) -> None:
        wavelength = self.assay_design.wavelength.to("nanometer").value
        if wavelength != ASSAY_WAVELENGTH_NANOMETER:
            raise ValueError(
                f"V0 assay wavelength is fixed at {ASSAY_WAVELENGTH_NANOMETER} nanometer"
            )
        maximum_well_volume = self.assay_design.maximum_well_volume.to("microliter").value
        if maximum_well_volume > ASSAY_MAXIMUM_WELL_VOLUME_MICROLITER:
            raise ValueError("assay maximum well volume exceeds the V0 simulator/device limit")
        reagent = self.parameter_space.parameters["reagent_volume"]
        sample = self.parameter_space.parameters["sample_volume"]
        reagent_maximum = QuantityValue(value=reagent.maximum, unit=reagent.unit).to("microliter")
        sample_maximum = QuantityValue(value=sample.maximum, unit=sample.unit).to("microliter")
        if reagent_maximum.value + sample_maximum.value > maximum_well_volume:
            raise ValueError("maximum reagent and sample volumes exceed configured well capacity")

    def _validate_grid_budget(self) -> None:
        if self.optimization.strategy != "grid":
            return
        grid_size = 1
        for name, parameter in self.parameter_space.parameters.items():
            if parameter.resolution is None:
                raise ValueError(f"grid parameter {name!r} requires a resolution")
            regular_count = int((parameter.maximum - parameter.minimum) // parameter.resolution) + 1
            reaches_maximum = (
                parameter.minimum + (regular_count - 1) * parameter.resolution == parameter.maximum
            )
            grid_size *= regular_count if reaches_maximum else regular_count + 1
            if grid_size > self.optimization.maximum_grid_size:
                raise ValueError(
                    "grid contains more than maximum_grid_size="
                    f"{self.optimization.maximum_grid_size} combinations"
                )
        if self.campaign.max_experiments > grid_size:
            raise ValueError(
                f"campaign budget {self.campaign.max_experiments} exceeds finite grid size "
                f"{grid_size}"
            )


class ValidationPolicyConfig(DomainModel):
    """Trusted installation policy; a campaign cannot override this document."""

    schema_version: Literal["1"]
    policy_name: str
    policy_version: str
    allowed_materials: frozenset[str]
    allowed_command_types: frozenset[CommandType]
    required_control_types: frozenset[ControlType]
    minimum_dispense_volume: QuantityValue
    maximum_dispense_volume: QuantityValue
    maximum_well_volume: QuantityValue
    wavelength_range: QuantityRange
    minimum_blank_replicates: int = Field(ge=1)
    minimum_standard_levels: int = Field(ge=2)
    minimum_standard_replicates: int = Field(ge=1)
    minimum_control_replicates: int = Field(ge=1)
    supported_vessel_types: frozenset[str]

    @model_validator(mode="after")
    def _compatible_volume_limits(self) -> ValidationPolicyConfig:
        maximum = self.maximum_dispense_volume.to(self.minimum_dispense_volume.unit)
        well_maximum = self.maximum_well_volume.to(self.minimum_dispense_volume.unit)
        if self.minimum_dispense_volume.value >= maximum.value:
            raise ValueError("minimum dispense volume must be below maximum")
        if maximum.value > well_maximum.value:
            raise ValueError("maximum dispense volume cannot exceed maximum well volume")
        return self
