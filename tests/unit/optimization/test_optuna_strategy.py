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


def _native_trial_data(
    record: TrialRecord,
) -> tuple[
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
        "autolab_constraint_residuals": [float(observation.constraint_residuals["cv"])],
    }
    return params, distributions, values, attrs


# --------------------------------------------------------------- batch rounds


def _culture_problem() -> OptimizationProblem:
    """The culture objective: 3 objectives (OD up; energy, CO2 down) plus a constraint."""

    return OptimizationProblem(
        ParameterSpace(
            parameters={
                "temperature": ContinuousParameter(
                    unit="degC",
                    minimum=Decimal("20"),
                    maximum=Decimal("34"),
                    resolution=Decimal("0.1"),
                ),
                "light_intensity": ContinuousParameter(
                    unit="micromole / meter ** 2 / second",
                    minimum=Decimal("30"),
                    maximum=Decimal("400"),
                    resolution=Decimal("1"),
                ),
                "ph": ContinuousParameter(
                    unit="dimensionless",
                    minimum=Decimal("6.5"),
                    maximum=Decimal("8.5"),
                    resolution=Decimal("0.05"),
                ),
            }
        ),
        (
            ObjectiveMetric(
                name="final_od750", direction=ObjectiveDirection.MAXIMIZE, unit="dimensionless"
            ),
            ObjectiveMetric(
                name="energy_kwh", direction=ObjectiveDirection.MINIMIZE, unit="kilowatt_hour"
            ),
            ObjectiveMetric(name="co2_g", direction=ObjectiveDirection.MINIMIZE, unit="gram"),
        ),
        (
            MetricConstraint(
                metric="co2_g", operator=ConstraintOperator.LTE, threshold=Decimal("20")
            ),
        ),
    )


def _culture_feedback(problem: OptimizationProblem, proposal) -> StrategyFeedback:
    values = {name: float(value.value) for name, value in proposal.parameter_values.items()}
    observation = build_optimization_observation(
        problem=problem,
        campaign_id=proposal.campaign_id,
        experiment_id=proposal.experiment_id,
        result_id=uuid4(),
        metric_values={
            "final_od750": 2.0
            - abs(values["temperature"] - 28) / 10
            + values["light_intensity"] / 1000,
            "energy_kwh": values["light_intensity"] / 100,
            "co2_g": 40 - 4 * values["ph"],
        },
    )
    return StrategyFeedback.observed(observation, proposal.strategy_trial_id)


def _batch_strategy(problem: OptimizationProblem, storage_url: str | None = None) -> OptunaStrategy:
    return OptunaStrategy(
        campaign_id=UUID("0d7b6a8e-6c2f-4b8e-9a43-55a1e3c3f0aa"),
        problem=problem,
        seed=4000,
        startup_experiments=3,
        storage_url=storage_url,
        prefer_gp=False,
        batch_size=3,
    )


def _run_rounds(
    strategy: OptunaStrategy, problem: OptimizationProblem, rounds: int
) -> list[TrialRecord]:
    records: list[TrialRecord] = []
    for _ in range(rounds):
        proposals = [strategy.ask() for _ in range(3)]  # all asked before any is told
        for proposal in proposals:
            feedback = _culture_feedback(problem, proposal)
            strategy.tell(feedback)
            records.append(TrialRecord(proposal, feedback))
    return records


def test_constant_liar_round_on_the_culture_objective_is_distinct_and_in_bounds() -> None:
    import warnings

    from optuna.samplers import TPESampler

    problem = _culture_problem()
    strategy = _batch_strategy(problem)
    _run_rounds(strategy, problem, 1)  # random start-up
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        guided = [strategy.ask() for _ in range(3)]  # model-guided, two RUNNING liars
    sampler = strategy.study.sampler
    assert isinstance(sampler, TPESampler) and sampler._constant_liar
    assert [str(item.message) for item in caught] == []
    values = [tuple(sorted((k, v.value) for k, v in p.parameter_values.items())) for p in guided]
    assert len(set(values)) == 3
    for proposal in guided:
        problem.parameter_space.validate_values(proposal.parameter_values)


def test_sequential_strategy_does_not_use_constant_liar() -> None:
    from optuna.samplers import TPESampler

    strategy = OptunaStrategy(
        campaign_id=uuid4(), problem=_culture_problem(), seed=1, prefer_gp=False
    )
    strategy.ask()
    sampler = strategy.study.sampler
    assert isinstance(sampler, TPESampler) and not sampler._constant_liar


def test_batch_resume_mid_round_replays_rounds_and_matches_uninterrupted(tmp_path) -> None:
    problem = _culture_problem()
    full = _batch_strategy(problem, f"sqlite:///{tmp_path / 'full.db'}")
    full_records = _run_rounds(full, problem, 4)

    url = f"sqlite:///{tmp_path / 'resumed.db'}"
    interrupted = _batch_strategy(problem, url)
    records = _run_rounds(interrupted, problem, 2)
    round_three = [interrupted.ask() for _ in range(3)]
    first = _culture_feedback(problem, round_three[0])
    interrupted.tell(first)  # one slot finished; two still running at the crash
    records.append(TrialRecord(round_three[0], first))
    del interrupted

    resumed = _batch_strategy(problem, url)
    resumed.reconcile_pending_batch(records, round_three[1:])
    for proposal in round_three[1:]:
        feedback = _culture_feedback(problem, proposal)
        resumed.tell(feedback)
        records.append(TrialRecord(proposal, feedback))
    records.sort(key=lambda r: r.proposal.suggestion_index)
    records.extend(_run_rounds(resumed, problem, 1))

    assert _trial_snapshots(records) == _trial_snapshots(full_records)


def test_pending_proposals_outside_the_final_round_are_refused(tmp_path) -> None:
    problem = _culture_problem()
    url = f"sqlite:///{tmp_path / 'gap.db'}"
    strategy = _batch_strategy(problem, url)
    round_one = [strategy.ask() for _ in range(3)]
    records = []
    for proposal in round_one[1:]:
        feedback = _culture_feedback(problem, proposal)
        strategy.tell(feedback)
        records.append(TrialRecord(proposal, feedback))
    round_two = [strategy.ask() for _ in range(3)]
    for proposal in round_two:
        feedback = _culture_feedback(problem, proposal)
        strategy.tell(feedback)
        records.append(TrialRecord(proposal, feedback))
    del strategy
    with pytest.raises(OptunaReconciliationError, match="final round"):
        _batch_strategy(problem, url).reconcile_pending_batch(records, [round_one[0]])


def _startup_design(seed: int, count: int = 7) -> list[tuple[tuple[str, Decimal], ...]]:
    strategy = OptunaStrategy(
        campaign_id=UUID("2b0f7f0e-2d0c-4a43-9d3e-1d2a7d0c9a11"),
        problem=_problem(),
        seed=seed,
        startup_experiments=count,
        prefer_gp=False,
    )
    return [_parameter_snapshot(record) for record in _run_trials(strategy, count)]


def test_adjacent_campaign_seeds_do_not_share_their_startup_design() -> None:
    """Audit: with ``seed + index`` seed 11 replayed seed 10's design shifted by one."""

    ten, eleven = _startup_design(10), _startup_design(11)
    assert not set(ten) & set(eleven)
    assert _startup_design(10) == ten  # still reproducible


def test_sampler_seeds_are_derived_by_hashing_not_by_offset() -> None:
    seeds = {OptunaStrategy._sampler_seed(seed, index) for seed in range(20) for index in range(20)}  # pyright: ignore[reportPrivateUsage]
    assert len(seeds) == 400  # (seed, index) pairs no longer alias each other
    assert all(0 <= seed < 2**32 for seed in seeds)


def test_campaigns_checkpointed_before_the_change_keep_their_legacy_seeds() -> None:
    """Resuming an older campaign must replay the trials it actually ran."""

    campaign_id = uuid4()
    legacy = OptunaStrategy(
        campaign_id=campaign_id,
        problem=_problem(),
        seed=10,
        startup_experiments=5,
        prefer_gp=False,
    )
    state = legacy.checkpoint()
    old_data = dict(state.checkpoint_data)
    old_data.pop("sampler_seed_scheme")  # as written by strategy version 1.1.0
    restored = OptunaStrategy(
        campaign_id=campaign_id,
        problem=_problem(),
        seed=10,
        startup_experiments=5,
        prefer_gp=False,
        restored_state=state.model_copy(update={"checkpoint_data": old_data}),
    )
    assert restored.sampler_seed_scheme == "offset-v0"
    assert restored.checkpoint().checkpoint_data["sampler_seed_scheme"] == "offset-v0"
    assert legacy.sampler_seed_scheme == "sha256-v1"
    with pytest.raises(ValueError, match="seed scheme"):
        OptunaStrategy(
            campaign_id=campaign_id,
            problem=_problem(),
            seed=10,
            prefer_gp=False,
            restored_state=state.model_copy(
                update={"checkpoint_data": {**old_data, "sampler_seed_scheme": "unknown"}}
            ),
        )
