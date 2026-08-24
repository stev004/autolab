"""Simulation-only backend construction.

This module is deliberately imported lazily by the campaign service. Human
execution and physical-sensor processes must not import, inspect, or call the
hidden assay world.
"""

from __future__ import annotations

from autonomous_lab.campaigns.config import CampaignConfig
from autonomous_lab.domain.experiments import ExperimentProposal
from autonomous_lab.hardware.simulated import SimulatedExperimentPlan, SimulatedHardwareBackend
from autonomous_lab.simulation.assay_model import (
    AssayDesign,
    CostModel,
    ExecutedAssayConditions,
    FailureInjection,
    NonlinearAssaySimulator,
    SimulationConfig,
)
from autonomous_lab.simulation.assay_world import AssayWorld


async def build_simulated_backend(
    config: CampaignConfig,
    proposal: ExperimentProposal,
    *,
    failures: object | None = None,
) -> SimulatedHardwareBackend:
    """Construct the V0 hidden-world backend for simulation mode only."""

    if config.execution.mode != "simulation":
        raise ValueError("the simulated backend is unavailable outside simulation mode")
    values = proposal.parameter_values
    conditions = ExecutedAssayConditions(
        reagent_concentration_mg_ml=float(
            values["reagent_concentration"].to("milligram / milliliter").value
        ),
        reagent_volume_microliter=float(values["reagent_volume"].to("microliter").value),
        sample_volume_microliter=float(values["sample_volume"].to("microliter").value),
        incubation_minutes=float(values["incubation_time"].to("minute").value),
        temperature_celsius=float(values["temperature"].to("degree_Celsius").value),
    )
    design_config = config.assay_design
    design = AssayDesign(
        blank_replicates=design_config.blank_replicates,
        standard_levels=tuple(float(item) for item in design_config.standard_levels),
        standard_replicates=design_config.standard_replicates,
        control_level=float(design_config.control_level),
        control_replicates=design_config.control_replicates,
        check_standard_level=float(design_config.check_standard_level),
        check_standard_replicates=design_config.check_standard_replicates,
        wavelength_nanometer=float(design_config.wavelength.to("nanometer").value),
    )
    simulation = config.simulation
    cost = simulation.cost
    simulator = NonlinearAssaySimulator(
        SimulationConfig(
            seed=simulation.seed,
            outlier_probability=float(simulation.outlier_probability),
            cost=CostModel(
                currency="GBP",
                fixed_consumables_per_well=float(cost.fixed_consumables.to("GBP").value),
                reagent_per_mg=float(cost.reagent_per_milligram.to("GBP / milligram").value),
                sample_per_microliter=float(
                    cost.sample_per_microliter.to("GBP / microliter").value
                ),
                incubation_per_minute=float(
                    cost.incubation_per_minute.to("GBP / minute").value
                ),
            ),
        )
    )
    return await SimulatedHardwareBackend.create(
        SimulatedExperimentPlan(
            experiment_id=proposal.experiment_id,
            experiment_ordinal=proposal.suggestion_index,
            campaign_seed=config.campaign.seed,
            conditions=conditions,
            design=design,
            failures=(
                FailureInjection()
                if failures is None
                else _require_failure_injection(failures)
            ),
        ),
        world=AssayWorld(simulator=simulator),
    )


def _require_failure_injection(value: object) -> FailureInjection:
    if not isinstance(value, FailureInjection):
        raise TypeError("simulation failure provider returned an invalid value")
    return value
