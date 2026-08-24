from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from optuna.distributions import FloatDistribution
from optuna.trial import TrialState, create_trial

from autonomous_lab.domain.enums import ConstraintOperator, ObjectiveDirection
from autonomous_lab.domain.objectives import MetricConstraint, ObjectiveMetric
from autonomous_lab.domain.parameters import ContinuousParameter, ParameterSpace
from autonomous_lab.optimization import (
    DuplicateFeedbackError,
    FeedbackDisposition,
    OptimizationProblem,
    OptunaReconciliationError,
    OptunaStrategy,
    StrategyFeedback,
    TrialRecord,
    build_optimization_observation,
)


def _problem() -> OptimizationProblem:
    return OptimizationProblem(
        ParameterSpace(
            parameters={
                "x": ContinuousParameter(
                    unit="dimensionless", minimum=Decimal("0"), maximum=Decimal("1")
                ),
                "volume": ContinuousParameter(
                    unit="microliter", minimum=Decimal("20"), maximum=Decimal("200")
                ),
            }
        ),
        (
            ObjectiveMetric(name="cost", direction=ObjectiveDirection.MINIMIZE, unit="GBP"),
            ObjectiveMetric(
                name="consumption",
                direction=ObjectiveDirection.MINIMIZE,
                unit="microliter",
            ),
        ),
        (
            MetricConstraint(
                metric="cv",
                operator=ConstraintOperator.LTE,
                threshold=Decimal("0.05"),
            ),
        ),
    )


def _feedback(proposal, *, cv: float = 0.04) -> StrategyFeedback:
    observation = build_optimization_observation(
        problem=_problem(),
        campaign_id=proposal.campaign_id,
        experiment_id=proposal.experiment_id,
        result_id=uuid4(),
        metric_values={
            "cost": 0.2 + float(proposal.parameter_values["x"].value),
            "consumption": float(proposal.parameter_values["volume"].value),
            "cv": cv,
        },
    )
    return StrategyFeedback.observed(observation, proposal.strategy_trial_id)


def test_optuna_external_ask_tell_records_constraints_and_infeasible_results() -> None:
    strategy = OptunaStrategy(
        campaign_id=uuid4(),
        problem=_problem(),
        seed=10,
        startup_experiments=1,
        prefer_gp=False,
    )
    feasible = strategy.ask()
    infeasible = strategy.ask()

    assert strategy.tell(_feedback(feasible))
    assert strategy.tell(_feedback(infeasible, cv=0.09))

    trials = strategy.study.trials
    assert [trial.state for trial in trials] == [TrialState.COMPLETE, TrialState.COMPLETE]
    assert trials[0].user_attrs["autolab_constraint_residuals"] == pytest.approx([-0.01])
    assert trials[1].user_attrs["autolab_constraint_residuals"] == pytest.approx([0.04])
    assert strategy.checkpoint().observation_count == 2
    assert strategy.sampler_name == "optuna_tpe"


def test_optuna_marks_integrity_failure_failed_without_objective_values() -> None:
    strategy = OptunaStrategy(campaign_id=uuid4(), problem=_problem(), seed=3, prefer_gp=False)
    proposal = strategy.ask()
    feedback = StrategyFeedback.excluded(
        proposal.strategy_trial_id,
        FeedbackDisposition.QC_FAILED,
        reason="missing reading",
    )

    assert not strategy.tell(feedback)
    trial = strategy.study.trials[0]
    assert trial.state is TrialState.FAIL
    assert trial.values is None
    assert strategy.checkpoint().observation_count == 0


def test_authoritative_reconciliation_replays_trials_once() -> None:
    campaign_id = uuid4()
    source = OptunaStrategy(campaign_id=campaign_id, problem=_problem(), seed=8, prefer_gp=False)
    observed_proposal = source.ask()
    observed_feedback = _feedback(observed_proposal)
    source.tell(observed_feedback)
    failed_proposal = source.ask()
    failed_feedback = StrategyFeedback.excluded(
        failed_proposal.strategy_trial_id,
        FeedbackDisposition.EXECUTION_FAILED,
        reason="device timeout",
    )
    source.tell(failed_feedback)
    records = (
        TrialRecord(observed_proposal, observed_feedback),
        TrialRecord(failed_proposal, failed_feedback),
    )

    recovered = OptunaStrategy(campaign_id=campaign_id, problem=_problem(), seed=8, prefer_gp=False)
    recovered.reconcile(records)
    recovered.reconcile(records)

    assert [trial.state for trial in recovered.study.trials] == [
        TrialState.COMPLETE,
        TrialState.FAIL,
    ]
    assert recovered.checkpoint().observation_count == 1
    assert len(recovered.pareto_points()) == 1
    assert recovered.ask().suggestion_index == 2
    with pytest.raises(DuplicateFeedbackError):
        recovered.tell(observed_feedback)


def test_sqlite_study_resumes_after_authoritative_reconciliation(tmp_path) -> None:
    campaign_id = uuid4()
    storage_url = f"sqlite:///{tmp_path / 'optuna.db'}"
    first = OptunaStrategy(
        campaign_id=campaign_id,
        problem=_problem(),
        seed=5,
        storage_url=storage_url,
        prefer_gp=False,
    )
    proposal = first.ask()
    feedback = _feedback(proposal)
    first.tell(feedback)

    resumed = OptunaStrategy(
        campaign_id=campaign_id,
        problem=_problem(),
        seed=5,
        storage_url=storage_url,
        prefer_gp=False,
    )
    resumed.reconcile((TrialRecord(proposal, feedback),))

    assert len(resumed.study.trials) == 1
    assert resumed.ask().suggestion_index == 1
    assert resumed.checkpoint().checkpoint_data["storage_is_persistent"] is True


def test_sqlite_study_resumes_with_one_authoritative_pending_trial(tmp_path) -> None:
    campaign_id = uuid4()
    storage_url = f"sqlite:///{tmp_path / 'optuna-pending.db'}"
    first = OptunaStrategy(
        campaign_id=campaign_id,
        problem=_problem(),
        seed=17,
        storage_url=storage_url,
        prefer_gp=False,
    )
    pending = first.ask()

    resumed = OptunaStrategy(
        campaign_id=campaign_id,
        problem=_problem(),
        seed=17,
        storage_url=storage_url,
        prefer_gp=False,
        restored_state=first.checkpoint(),
    )
    resumed.reconcile_pending((), pending)
    assert resumed.study.trials[0].state is TrialState.RUNNING

    feedback = _feedback(pending)
    assert resumed.tell(feedback)
    assert resumed.study.trials[0].state is TrialState.COMPLETE
    assert resumed.ask().suggestion_index == 1


def test_reconciliation_repairs_matching_running_trial_from_authoritative_record() -> None:
    strategy = OptunaStrategy(campaign_id=uuid4(), problem=_problem(), seed=13, prefer_gp=False)
    proposal = strategy.ask()
    feedback = _feedback(proposal)

    strategy.reconcile((TrialRecord(proposal, feedback),))

    trial = strategy.study.trials[0]
    assert trial.state is TrialState.COMPLETE
    assert trial.values == pytest.approx(
        [
            float(feedback.observation.objective_values["cost"]),
            float(feedback.observation.objective_values["consumption"]),
        ]
    )
    assert trial.user_attrs["autolab_constraint_residuals"] == pytest.approx([-0.01])


def test_reconciliation_rejects_altered_native_parameter_and_distribution() -> None:
    source = OptunaStrategy(campaign_id=uuid4(), problem=_problem(), seed=21, prefer_gp=False)
    proposal = source.ask()
    feedback = _feedback(proposal)
    record = TrialRecord(proposal, feedback)
    expected = _native_trial_data(record)

    altered_parameter = OptunaStrategy(
        campaign_id=proposal.campaign_id, problem=_problem(), seed=21, prefer_gp=False
    )
    altered_parameter.study.add_trial(
        create_trial(
            state=TrialState.COMPLETE,
            params={**expected[0], "x": min(1.0, expected[0]["x"] + 0.1)},
            distributions=expected[1],
            values=expected[2],
            user_attrs=expected[3],
        )
    )
    with pytest.raises(OptunaReconciliationError, match="parameter 'x'"):
        altered_parameter.reconcile((record,))

    altered_distribution = OptunaStrategy(
        campaign_id=proposal.campaign_id, problem=_problem(), seed=21, prefer_gp=False
    )
    altered_distribution.study.add_trial(
        create_trial(
            state=TrialState.COMPLETE,
            params=expected[0],
            distributions={
                **expected[1],
                "x": FloatDistribution(low=0.0, high=2.0),
            },
            values=expected[2],
            user_attrs=expected[3],
        )
    )
    with pytest.raises(OptunaReconciliationError, match="distributions"):
        altered_distribution.reconcile((record,))


@pytest.mark.parametrize(
    ("values", "constraint_residuals", "message"),
    [
        ([999.0, 100.0], [-0.01], "objective values"),
        ([0.5, 100.0], None, "constraint metadata"),
        ([0.5, 100.0], [0.25], "constraint metadata"),
    ],
)
def test_reconciliation_rejects_altered_native_observation(
    values: list[float] | None,
    constraint_residuals: list[float] | None,
    message: str,
) -> None:
    source = OptunaStrategy(campaign_id=uuid4(), problem=_problem(), seed=22, prefer_gp=False)
    proposal = source.ask()
    feedback = _feedback(proposal)
    record = TrialRecord(proposal, feedback)
    params, distributions, expected_values, attrs = _native_trial_data(record)
    if values is not None and values[0] == 0.5:
        values = expected_values
    if constraint_residuals is None:
        attrs.pop("autolab_constraint_residuals")
    else:
        attrs["autolab_constraint_residuals"] = constraint_residuals

    recovered = OptunaStrategy(
        campaign_id=proposal.campaign_id, problem=_problem(), seed=22, prefer_gp=False
    )
    recovered.study.add_trial(
        create_trial(
            state=TrialState.COMPLETE,
            params=params,
            distributions=distributions,
            values=values,
            user_attrs=attrs,
        )
    )

    with pytest.raises(OptunaReconciliationError, match=message):
        recovered.reconcile((record,))


def test_reconciliation_fails_orphan_running_trial_and_reports_it() -> None:
    strategy = OptunaStrategy(campaign_id=uuid4(), problem=_problem(), seed=31, prefer_gp=False)
    strategy.ask()

    strategy.reconcile(())

    assert strategy.study.trials[0].state is TrialState.FAIL
    assert strategy.checkpoint().checkpoint_data["orphaned_native_trials"] == [0]


def test_reconciliation_rejects_terminal_native_trial_without_authoritative_record() -> None:
    strategy = OptunaStrategy(campaign_id=uuid4(), problem=_problem(), seed=32, prefer_gp=False)
    proposal = strategy.ask()
    strategy.tell(
        StrategyFeedback.excluded(
            proposal.strategy_trial_id,
            FeedbackDisposition.EXECUTION_FAILED,
            reason="injected",
        )
    )

    with pytest.raises(OptunaReconciliationError, match="no authoritative trial record"):
        strategy.reconcile(())


def test_sqlite_resume_reconstructs_sampler_and_matches_uninterrupted_trajectory(
    tmp_path,
) -> None:
    campaign_id = UUID("61a8c7d3-684d-4f62-89ca-cd56915b513e")
    full = OptunaStrategy(
        campaign_id=campaign_id,
        problem=_problem(),
        seed=917,
        startup_experiments=3,
        storage_url=f"sqlite:///{tmp_path / 'full-optuna.db'}",
        prefer_gp=False,
    )
    full_records = _run_trials(full, 20)

    paused = OptunaStrategy(
        campaign_id=campaign_id,
        problem=_problem(),
        seed=917,
        startup_experiments=3,
        storage_url=f"sqlite:///{tmp_path / 'resumed-optuna.db'}",
        prefer_gp=False,
    )
    resumed_records = _run_trials(paused, 5)
    del paused

    resumed = OptunaStrategy(
        campaign_id=campaign_id,
        problem=_problem(),
        seed=917,
        startup_experiments=3,
        storage_url=f"sqlite:///{tmp_path / 'resumed-optuna.db'}",
        prefer_gp=False,
    )
    resumed.reconcile(resumed_records)
    resumed_records.extend(_run_trials(resumed, 15))

    assert _trial_snapshots(resumed_records) == _trial_snapshots(full_records)
    assert len(set(_parameter_snapshot(record) for record in resumed_records)) == 20


def _run_trials(strategy: OptunaStrategy, count: int) -> list[TrialRecord]:
    records: list[TrialRecord] = []
    for _ in range(count):
        proposal = strategy.ask()
        feedback = _feedback(proposal)
        strategy.tell(feedback)
        records.append(TrialRecord(proposal, feedback))
    return records


def _parameter_snapshot(record: TrialRecord) -> tuple[tuple[str, Decimal], ...]:
    return tuple(
        (name, value.value) for name, value in sorted(record.proposal.parameter_values.items())
    )


def _trial_snapshots(
    records: list[TrialRecord],
) -> list[tuple[tuple[tuple[str, Decimal], ...], tuple[tuple[str, Decimal], ...]]]:
    snapshots = []
    for record in records:
        observation = record.feedback.observation
        assert observation is not None
        snapshots.append(
            (
                _parameter_snapshot(record),
                tuple(sorted(observation.objective_values.items())),
            )
        )
    return snapshots


def _native_trial_data(record: TrialRecord) -> tuple[
    dict[str, float],
    dict[str, FloatDistribution],
    list[float],
    dict[str, object],
]:
    proposal = record.proposal
    observation = record.feedback.observation
    assert observation is not None
    params = {
        "volume": float(proposal.parameter_values["volume"].value),
        "x": float(proposal.parameter_values["x"].value),
    }
    distributions = {
        "volume": FloatDistribution(low=20.0, high=200.0),
        "x": FloatDistribution(low=0.0, high=1.0),
    }
    values = [
        float(observation.objective_values["cost"]),
        float(observation.objective_values["consumption"]),
    ]
    attrs: dict[str, object] = {
        "autolab_executed_parameter_values": {
            name: str(value.value) for name, value in proposal.parameter_values.items()
        },
        "autolab_constraint_residuals": [
            float(observation.constraint_residuals["cv"])
        ],
    }
    return params, distributions, values, attrs
