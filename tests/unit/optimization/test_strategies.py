from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from autonomous_lab.domain.enums import (
    ConstraintOperator,
    ObjectiveDirection,
    ParameterScale,
)
from autonomous_lab.domain.objectives import MetricConstraint, ObjectiveMetric
from autonomous_lab.domain.optimization import OptimizationObservation
from autonomous_lab.domain.parameters import ContinuousParameter, ParameterSpace
from autonomous_lab.domain.quantities import QuantityValue
from autonomous_lab.optimization import (
    DuplicateFeedbackError,
    FeedbackDisposition,
    GridSearchStrategy,
    OptimizationProblem,
    RandomSearchStrategy,
    SearchSpaceExhausted,
    StrategyFeedback,
    build_optimization_observation,
)


def _problem(*, resolved: bool = False) -> OptimizationProblem:
    return OptimizationProblem(
        parameter_space=ParameterSpace(
            parameters={
                "concentration": ContinuousParameter(
                    unit="milligram / milliliter",
                    minimum=Decimal("0.1"),
                    maximum=Decimal("1.0"),
                    resolution=Decimal("0.3") if resolved else None,
                ),
                "time": ContinuousParameter(
                    unit="minute",
                    minimum=Decimal("1"),
                    maximum=Decimal("100"),
                    scale=ParameterScale.LOG,
                    resolution=Decimal("33") if resolved else None,
                ),
            }
        ),
        objective_metrics=(
            ObjectiveMetric(
                name="cost_per_test", direction=ObjectiveDirection.MINIMIZE, unit="GBP"
            ),
            ObjectiveMetric(
                name="reagent_consumption",
                direction=ObjectiveDirection.MINIMIZE,
                unit="milligram",
            ),
        ),
        performance_constraints=(
            MetricConstraint(
                metric="control_cv",
                operator=ConstraintOperator.LTE,
                threshold=Decimal("0.05"),
            ),
            MetricConstraint(
                metric="signal_to_noise",
                operator=ConstraintOperator.GTE,
                threshold=Decimal("10"),
            ),
        ),
    )


def _observation(proposal, *, feasible: bool = True) -> OptimizationObservation:
    return build_optimization_observation(
        problem=_problem(),
        campaign_id=proposal.campaign_id,
        experiment_id=proposal.experiment_id,
        result_id=uuid4(),
        metric_values={
            "cost_per_test": 0.1 + 0.01 * proposal.suggestion_index,
            "reagent_consumption": 0.2 - 0.01 * proposal.suggestion_index,
            "control_cv": 0.04 if feasible else 0.08,
            "signal_to_noise": 12 if feasible else 8,
        },
    )


def test_seeded_random_search_reproduces_parameters_and_ids() -> None:
    campaign_id = uuid4()
    first = RandomSearchStrategy(campaign_id=campaign_id, problem=_problem(), seed=81)
    second = RandomSearchStrategy(campaign_id=campaign_id, problem=_problem(), seed=81)

    first_proposals = [first.ask() for _ in range(4)]
    second_proposals = [second.ask() for _ in range(4)]

    assert [item.parameter_values for item in first_proposals] == [
        item.parameter_values for item in second_proposals
    ]
    assert [item.proposal_id for item in first_proposals] == [
        item.proposal_id for item in second_proposals
    ]
    assert all(
        set(proposal.parameter_values) == {"concentration", "time"} for proposal in first_proposals
    )


def test_random_checkpoint_resumes_at_same_deterministic_suggestion() -> None:
    campaign_id = uuid4()
    original = RandomSearchStrategy(campaign_id=campaign_id, problem=_problem(), seed=12)
    original.ask()
    original.ask()
    state = original.checkpoint()

    resumed = RandomSearchStrategy(
        campaign_id=campaign_id,
        problem=_problem(),
        seed=12,
        restored_state=state,
    )
    resumed_third = resumed.ask()
    fresh = RandomSearchStrategy(campaign_id=campaign_id, problem=_problem(), seed=12)
    fresh.ask()
    fresh.ask()
    fresh_third = fresh.ask()

    assert resumed_third.parameter_values == fresh_third.parameter_values
    assert resumed_third.experiment_id == fresh_third.experiment_id


def test_failed_runs_are_terminal_but_not_learned() -> None:
    strategy = RandomSearchStrategy(campaign_id=uuid4(), problem=_problem(), seed=1)
    proposal = strategy.ask()
    feedback = StrategyFeedback.excluded(
        proposal.strategy_trial_id,
        FeedbackDisposition.QC_FAILED,
        reason="contaminated blank",
    )

    assert not strategy.tell(feedback)
    state = strategy.checkpoint()
    assert state.observation_count == 0
    assert state.checkpoint_data["terminal_trials"] == {proposal.strategy_trial_id: "qc_failed"}
    with pytest.raises(DuplicateFeedbackError):
        strategy.tell(feedback)


def test_scientifically_infeasible_qc_valid_result_is_learned() -> None:
    strategy = RandomSearchStrategy(campaign_id=uuid4(), problem=_problem(), seed=1)
    proposal = strategy.ask()
    observation = _observation(proposal, feasible=False)

    learned = strategy.tell(StrategyFeedback.observed(observation, proposal.strategy_trial_id))

    assert learned
    assert not observation.feasible
    assert observation.constraint_residuals["control_cv"] > 0
    assert strategy.checkpoint().observation_count == 1


def test_qc_ineligible_observation_is_rejected() -> None:
    strategy = RandomSearchStrategy(campaign_id=uuid4(), problem=_problem(), seed=1)
    proposal = strategy.ask()
    valid = _observation(proposal)
    invalid = valid.model_copy(update={"qc_eligible": False})

    with pytest.raises(ValueError, match="QC-ineligible"):
        strategy.tell(StrategyFeedback.observed(invalid, proposal.strategy_trial_id))


def test_feasible_observations_produce_pareto_data_in_objective_order() -> None:
    strategy = RandomSearchStrategy(campaign_id=uuid4(), problem=_problem(), seed=1)
    proposals = [strategy.ask() for _ in range(3)]
    metric_pairs = ((0.1, 0.3), (0.2, 0.2), (0.3, 0.4))
    for proposal, (cost, reagent) in zip(proposals, metric_pairs, strict=True):
        observation = build_optimization_observation(
            problem=_problem(),
            campaign_id=proposal.campaign_id,
            experiment_id=proposal.experiment_id,
            result_id=uuid4(),
            metric_values={
                "cost_per_test": cost,
                "reagent_consumption": reagent,
                "control_cv": 0.04,
                "signal_to_noise": 12,
            },
        )
        strategy.tell(StrategyFeedback.observed(observation, proposal.strategy_trial_id))

    assert {point.objectives for point in strategy.pareto_points()} == {(0.1, 0.3), (0.2, 0.2)}


def test_grid_uses_exact_decimal_levels_and_stops_when_exhausted() -> None:
    strategy = GridSearchStrategy(
        campaign_id=uuid4(),
        problem=_problem(resolved=True),
        seed=0,
        maximum_grid_size=20,
    )

    proposals = [strategy.ask() for _ in range(strategy.grid_size)]

    assert strategy.grid_size == 16
    assert proposals[0].parameter_values["concentration"].value == Decimal("0.1")
    assert proposals[-1].parameter_values["concentration"].value == Decimal("1.0")
    assert proposals[-1].parameter_values["time"].value == Decimal("100")
    with pytest.raises(SearchSpaceExhausted):
        strategy.ask()


def test_grid_rejects_missing_levels_and_combinatorial_explosion() -> None:
    with pytest.raises(ValueError, match="requires a resolution"):
        GridSearchStrategy(campaign_id=uuid4(), problem=_problem(), seed=0)

    with pytest.raises(ValueError, match="limit"):
        GridSearchStrategy(
            campaign_id=uuid4(),
            problem=_problem(resolved=True),
            seed=0,
            maximum_grid_size=10,
        )

    extremely_dense = OptimizationProblem(
        parameter_space=ParameterSpace(
            parameters={
                "x": ContinuousParameter(
                    unit="dimensionless",
                    minimum=Decimal("0"),
                    maximum=Decimal("1"),
                    resolution=Decimal("0.000000001"),
                )
            }
        ),
        objective_metrics=_problem().objective_metrics,
        performance_constraints=_problem().performance_constraints,
    )
    with pytest.raises(ValueError, match="levels"):
        GridSearchStrategy(
            campaign_id=uuid4(),
            problem=extremely_dense,
            seed=0,
            maximum_grid_size=100,
        )


def test_proposal_parameters_are_quantities_not_operations() -> None:
    strategy = RandomSearchStrategy(campaign_id=uuid4(), problem=_problem(), seed=4)
    proposal = strategy.ask()

    assert all(isinstance(value, QuantityValue) for value in proposal.parameter_values.values())
    assert not hasattr(proposal, "steps")
