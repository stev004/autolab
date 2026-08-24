"""Factory that wires one coherent set of simulated devices."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID

from autonomous_lab.domain.quantities import QuantityValue
from autonomous_lab.hardware.commands import MaterialKind
from autonomous_lab.hardware.evidence import (
    DefaultEvidenceFinalizer,
    EvidenceFinalizer,
    LinearAssayCostCalculator,
)
from autonomous_lab.hardware.registry import DeviceRegistry
from autonomous_lab.hardware.simulated.incubator import SimulatedIncubator
from autonomous_lab.hardware.simulated.liquid_handler import SimulatedLiquidHandler
from autonomous_lab.hardware.simulated.spectrophotometer import SimulatedSpectrophotometer
from autonomous_lab.hardware.simulated.state import SimulatedRunState
from autonomous_lab.simulation.assay_model import (
    AssayDesign,
    ExecutedAssayConditions,
    FailureInjection,
)
from autonomous_lab.simulation.assay_world import AssayWorld


@dataclass(frozen=True, slots=True)
class SimulatedExperimentPlan:
    experiment_id: UUID
    experiment_ordinal: int
    campaign_seed: int
    conditions: ExecutedAssayConditions
    design: AssayDesign = field(default_factory=AssayDesign)
    failures: FailureInjection = field(default_factory=FailureInjection)
    measurement_vessel_ids: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.experiment_ordinal < 0 or self.campaign_seed < 0:
            raise ValueError("experiment ordinal and campaign seed must be non-negative")
        if self.measurement_vessel_ids is not None and len(self.measurement_vessel_ids) != len(
            set(self.measurement_vessel_ids)
        ):
            raise ValueError("measurement vessel IDs must be unique")


@dataclass(slots=True)
class SimulatedHardwareBackend:
    registry: DeviceRegistry
    state: SimulatedRunState
    liquid_handler: SimulatedLiquidHandler
    incubator: SimulatedIncubator
    spectrophotometer: SimulatedSpectrophotometer
    evidence_finalizer: EvidenceFinalizer
    backend_name: str = "simulated"
    backend_version: str = "1"

    @classmethod
    async def create(
        cls,
        plan: SimulatedExperimentPlan,
        *,
        world: AssayWorld | None = None,
    ) -> SimulatedHardwareBackend:
        material_kinds: dict[str, MaterialKind] = {
            "reagent": "reagent",
            "blank": "blank",
            "positive-control": "control",
            "check-standard": "standard",
        }
        material_properties: dict[str, dict[str, QuantityValue]] = {
            "reagent": {
                "concentration": QuantityValue(
                    value=Decimal(str(plan.conditions.reagent_concentration_mg_ml)),
                    unit="milligram / milliliter",
                )
            },
            "blank": {"nominal_level": QuantityValue(value=Decimal(0), unit="dimensionless")},
            "positive-control": {
                "nominal_level": QuantityValue(
                    value=Decimal(str(plan.design.control_level)),
                    unit="dimensionless",
                )
            },
        }
        for index, level in enumerate(plan.design.standard_levels, start=1):
            material_id = f"calibration-standard-{index:02d}"
            material_kinds[material_id] = "standard"
            material_properties[material_id] = {
                "nominal_level": QuantityValue(
                    value=Decimal(str(level)),
                    unit="dimensionless",
                )
            }
        if plan.design.check_standard_level is not None:
            material_properties["check-standard"] = {
                "nominal_level": QuantityValue(
                    value=Decimal(str(plan.design.check_standard_level)),
                    unit="dimensionless",
                )
            }
        assay_world = world or AssayWorld()
        state = SimulatedRunState(
            experiment_id=plan.experiment_id,
            world=assay_world,
            failures=plan.failures,
            material_kinds=material_kinds,
            material_properties=material_properties,
        )
        liquid_handler = SimulatedLiquidHandler(state)
        incubator = SimulatedIncubator(state)
        spectrophotometer = SimulatedSpectrophotometer(
            state,
            design=plan.design,
            experiment_ordinal=plan.experiment_ordinal,
            campaign_seed=plan.campaign_seed,
            measurement_vessel_ids=plan.measurement_vessel_ids,
        )
        registry = await DeviceRegistry.create((liquid_handler, incubator, spectrophotometer))
        cost = assay_world.simulator.config.cost
        evidence_finalizer = DefaultEvidenceFinalizer(
            LinearAssayCostCalculator(
                fixed_consumables_per_well=Decimal(str(cost.fixed_consumables_per_well)),
                reagent_per_milligram=Decimal(str(cost.reagent_per_mg)),
                sample_per_microliter=Decimal(str(cost.sample_per_microliter)),
                incubation_per_minute=Decimal(str(cost.incubation_per_minute)),
                currency_unit=cost.currency,
            )
        )
        return cls(
            registry=registry,
            state=state,
            liquid_handler=liquid_handler,
            incubator=incubator,
            spectrophotometer=spectrophotometer,
            evidence_finalizer=evidence_finalizer,
        )
