from __future__ import annotations

import pytest

from autonomous_lab.analysis.metrics import analyze_simulated_batch
from autonomous_lab.simulation.assay_model import (
    AssayDesign,
    ExecutedAssayConditions,
    FailureInjection,
    NonlinearAssaySimulator,
    SimulationConfig,
    SimulationExecutionError,
    WellRole,
)


def _conditions(**overrides: float) -> ExecutedAssayConditions:
    values = {
        "reagent_concentration_mg_ml": 0.55,
        "reagent_volume_microliter": 100.0,
        "sample_volume_microliter": 55.0,
        "incubation_minutes": 15.0,
        "temperature_celsius": 29.5,
    }
    values.update(overrides)
    return ExecutedAssayConditions(**values)


def _simulate(**kwargs: object):
    simulator = NonlinearAssaySimulator(SimulationConfig(outlier_probability=0, batch_drift_sd=0))
    return simulator.simulate_batch(
        experiment_id="experiment-7",
        experiment_ordinal=7,
        campaign_seed=41027,
        conditions=_conditions(),
        design=AssayDesign(),
        **kwargs,
    )


def test_complete_batch_has_required_roles_and_replicates() -> None:
    batch = _simulate()

    assert len(batch.measurements) == 26
    assert sum(item.role is WellRole.BLANK for item in batch.measurements) == 3
    assert sum(item.role is WellRole.STANDARD for item in batch.measurements) == 18
    assert sum(item.role is WellRole.CONTROL for item in batch.measurements) == 4
    assert sum(item.role is WellRole.CHECK_STANDARD for item in batch.measurements) == 1
    assert {
        item.analyte_concentration for item in batch.measurements if item.role is WellRole.STANDARD
    } == {
        0.0,
        0.2,
        0.4,
        0.6,
        0.8,
        1.0,
    }
    assert len({item.measurement_id for item in batch.measurements}) == 26


def test_configurable_check_standard_replicates_match_campaign_layout() -> None:
    simulator = NonlinearAssaySimulator(SimulationConfig(outlier_probability=0))
    batch = simulator.simulate_batch(
        experiment_id="two-checks",
        experiment_ordinal=1,
        campaign_seed=7,
        conditions=_conditions(),
        design=AssayDesign(check_standard_replicates=2),
    )

    checks = [item for item in batch.measurements if item.role is WellRole.CHECK_STANDARD]
    assert len(batch.measurements) == 27
    assert [item.well_id for item in checks] == ["CHK-01", "CHK-02"]


def test_fixed_identifiers_and_seed_produce_identical_raw_readings() -> None:
    first = _simulate()
    second = _simulate()

    assert first == second


def test_experiment_identifier_does_not_change_the_scientific_random_stream() -> None:
    simulator = NonlinearAssaySimulator(SimulationConfig(outlier_probability=0))
    common = dict(
        experiment_ordinal=1,
        campaign_seed=11,
        conditions=_conditions(),
        design=AssayDesign(),
    )
    first = simulator.simulate_batch(experiment_id="a", **common)
    second = simulator.simulate_batch(experiment_id="b", **common)

    assert [item.absorbance for item in first.measurements] == [
        item.absorbance for item in second.measurements
    ]
    assert [item.stream_id for item in first.measurements] == [
        item.stream_id for item in second.measurements
    ]
    assert [item.measurement_id for item in first.measurements] != [
        item.measurement_id for item in second.measurements
    ]


def test_simulator_seed_is_part_of_random_stream_provenance() -> None:
    common = dict(
        experiment_id="same",
        experiment_ordinal=1,
        campaign_seed=11,
        conditions=_conditions(),
        design=AssayDesign(),
    )
    first = NonlinearAssaySimulator(SimulationConfig(seed=1, outlier_probability=0)).simulate_batch(
        **common
    )
    second = NonlinearAssaySimulator(
        SimulationConfig(seed=2, outlier_probability=0)
    ).simulate_batch(**common)

    assert [item.absorbance for item in first.measurements] != [
        item.absorbance for item in second.measurements
    ]
    assert first.simulator_config_hash != second.simulator_config_hash


def test_cost_and_consumption_use_executed_quantities() -> None:
    simulator = NonlinearAssaySimulator(SimulationConfig(outlier_probability=0))
    conditions = _conditions(
        reagent_concentration_mg_ml=0.4,
        reagent_volume_microliter=50.0,
        sample_volume_microliter=30.0,
        incubation_minutes=10.0,
    )
    batch = simulator.simulate_batch(
        experiment_id="cost",
        experiment_ordinal=1,
        campaign_seed=2,
        conditions=conditions,
    )

    assert conditions.reagent_amount_mg_per_well == pytest.approx(0.02)
    assert batch.reagent_amount_mg_per_test == pytest.approx(0.02)
    assert batch.reagent_volume_microliter_per_test == pytest.approx(50)
    assert batch.total_reagent_consumption_mg == pytest.approx(0.02 * batch.executed_well_count)
    assert batch.total_reagent_volume_microliter == pytest.approx(50 * batch.executed_well_count)
    assert batch.cost_per_test == pytest.approx(0.018 + 1.8 * 0.02 + 0.00015 * 30 + 0.0002 * 10)


def test_response_has_diminishing_returns() -> None:
    simulator = NonlinearAssaySimulator(
        SimulationConfig(
            outlier_probability=0,
            gaussian_noise_sd=0,
            well_effect_sd=0,
            batch_drift_sd=0,
        )
    )

    def slope(volume: float) -> float:
        batch = simulator.simulate_batch(
            experiment_id=f"v-{volume}",
            experiment_ordinal=1,
            campaign_seed=2,
            conditions=_conditions(reagent_volume_microliter=volume),
        )
        return analyze_simulated_batch(batch).sensitivity

    low, middle, high = slope(30), slope(100), slope(190)
    assert middle > low
    assert high > middle
    assert high - middle < middle - low


def test_temperature_optimum_and_ratio_interaction_are_observable_only_through_readings() -> None:
    simulator = NonlinearAssaySimulator(
        SimulationConfig(
            outlier_probability=0,
            gaussian_noise_sd=0,
            well_effect_sd=0,
            batch_drift_sd=0,
        )
    )

    def observed_slope(identifier: str, conditions: ExecutedAssayConditions) -> float:
        batch = simulator.simulate_batch(
            experiment_id=identifier,
            experiment_ordinal=1,
            campaign_seed=2,
            conditions=conditions,
        )
        return analyze_simulated_batch(batch).sensitivity

    optimal_temperature = observed_slope("temp-opt", _conditions(temperature_celsius=29.5))
    cold = observed_slope("temp-cold", _conditions(temperature_celsius=18))
    hot = observed_slope("temp-hot", _conditions(temperature_celsius=40))
    balanced_ratio = observed_slope("ratio-balanced", _conditions(sample_volume_microliter=58))
    extreme_ratio = observed_slope("ratio-extreme", _conditions(sample_volume_microliter=10))

    assert optimal_temperature > cold
    assert optimal_temperature > hot
    assert balanced_ratio > extreme_ratio


@pytest.mark.parametrize(
    ("failure", "kind", "retry_safe", "ambiguous"),
    [
        (FailureInjection(failed_incubation=True), "failed_incubation", False, False),
        (
            FailureInjection(spectrophotometer_timeout=True),
            "spectrophotometer_timeout",
            True,
            False,
        ),
        (FailureInjection(ambiguous_dispense_failure=True), "ambiguous_dispense", False, True),
    ],
)
def test_execution_failures_expose_retry_semantics(
    failure: FailureInjection, kind: str, retry_safe: bool, ambiguous: bool
) -> None:
    with pytest.raises(SimulationExecutionError) as caught:
        _simulate(failures=failure)

    assert caught.value.failure_kind == kind
    assert caught.value.retry_safe is retry_safe
    assert caught.value.ambiguous is ambiguous


def test_missing_reading_remains_in_raw_batch_with_failure_status() -> None:
    batch = _simulate(failures=FailureInjection(missing_well_id="CTL-02"))
    missing = next(item for item in batch.measurements if item.well_id == "CTL-02")

    assert missing.absorbance is None
    assert missing.status == "missing"
