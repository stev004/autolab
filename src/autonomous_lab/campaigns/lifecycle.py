"""Experiment-type-agnostic campaign bookkeeping shared by assay and culture campaigns.

Everything here depends only on the evidence ledger (SQLite) and the optimiser
interface, never on what kind of experiment a campaign runs. Moving it out of
``CampaignService`` lets the culture campaign reuse the *same* resume rules,
trial reconciliation, optimiser outbox and checkpointing instead of a copy that
could drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import select

from autonomous_lab.analysis.pareto import ParetoPoint, pareto_front
from autonomous_lab.campaigns.config import ObjectiveConfig, OptimizationConfig
from autonomous_lab.domain.enums import CampaignState, ExperimentState
from autonomous_lab.domain.experiments import ExperimentProposal
from autonomous_lab.domain.optimization import OptimizationObservation, OptimizationState
from autonomous_lab.domain.parameters import ParameterSpace
from autonomous_lab.domain.state_machines import transition_campaign, transition_experiment
from autonomous_lab.optimization import (
    FeedbackDisposition,
    GridSearchStrategy,
    OptimizationProblem,
    OptimizationStrategy,
    OptunaStrategy,
    RandomSearchStrategy,
    StrategyFeedback,
    TrialRecord,
)
from autonomous_lab.provenance import capture_runtime_metadata
from autonomous_lab.storage import Storage, UnitOfWork
from autonomous_lab.storage.orm import (
    CampaignRecord,
    ExperimentRecord,
    ExperimentResultRecord,
    OptimizerObservationRecord,
    OptimizerRunRecord,
    ProposalRecord,
)


class CampaignExecutionError(RuntimeError):
    """Persisted campaign state cannot safely be resumed automatically."""


def optimizer_run_id(campaign_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"autolab:{campaign_id}:optimizer")


def result_id(experiment_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"autolab:{experiment_id}:result")


def transition_campaign_row(
    uow: UnitOfWork,
    campaign_id: UUID,
    current: CampaignState,
    target: CampaignState,
    *,
    reason: str | None = None,
) -> None:
    transition = transition_campaign(campaign_id, current, target, reason=reason)
    uow.campaigns.transition(
        str(campaign_id),
        target,
        expected_state=current.value,
        reason=transition.reason,
        details=transition.metadata,
        occurred_at=transition.occurred_at,
    )


def transition_experiment_row(
    uow: UnitOfWork,
    experiment_id: UUID,
    current: ExperimentState,
    target: ExperimentState,
    *,
    reason: str | None = None,
    metadata: dict[str, object] | None = None,
) -> None:
    transition = transition_experiment(
        experiment_id,
        current,
        target,
        reason=reason,
        metadata=metadata,
    )
    uow.experiments.transition(
        str(experiment_id),
        target,
        expected_state=current.value,
        reason=transition.reason,
        details=transition.metadata,
        failure_category=(
            None if metadata is None else str(metadata.get("failure_category") or "") or None
        ),
        failure_message=reason if target is ExperimentState.FAILED else None,
        occurred_at=transition.occurred_at,
    )


def next_checkpoint_ordinal(
    uow: UnitOfWork,
    optimizer_run_id: str,
    preferred: int,
) -> int:
    """Append state changes even when ask/tell share a suggestion count."""

    latest = uow.optimization.latest_checkpoint(optimizer_run_id)
    return preferred if latest is None else max(preferred, latest.ordinal + 1)


def optimizer_storage_url(
    optimizer_root: Path, campaign_id: UUID, strategy_name: str
) -> str | None:
    if strategy_name != "bayesian":
        return None
    optimizer_root.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{optimizer_root / f'{campaign_id}.sqlite3'}"


def build_strategy(
    *,
    campaign_id: UUID,
    parameter_space: ParameterSpace,
    objective: ObjectiveConfig,
    optimization: OptimizationConfig,
    seed: int,
    optimizer_run: OptimizerRunRecord,
    checkpoint: OptimizationState | None,
    prefer_gp: bool,
) -> OptimizationStrategy:
    problem = OptimizationProblem.from_models(parameter_space, objective)
    if optimization.strategy == "random":
        return RandomSearchStrategy(
            campaign_id=campaign_id,
            problem=problem,
            seed=seed,
            restored_state=checkpoint,
        )
    if optimization.strategy == "grid":
        return GridSearchStrategy(
            campaign_id=campaign_id,
            problem=problem,
            seed=seed,
            restored_state=checkpoint,
            maximum_grid_size=optimization.maximum_grid_size,
        )
    return OptunaStrategy(
        campaign_id=campaign_id,
        problem=problem,
        seed=seed,
        restored_state=checkpoint,
        startup_experiments=optimization.startup_experiments,
        storage_url=optimizer_run.external_storage_uri,
        prefer_gp=prefer_gp,
    )


class CampaignLedger:
    """Storage-backed campaign lifecycle operations, independent of experiment type."""

    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    def create_campaign(
        self,
        *,
        name: str,
        schema_version: str,
        raw_text: str,
        config: Any,
        config_sha256: str,
        source_sha256: str,
        objective: ObjectiveConfig,
        parameter_space: ParameterSpace,
        optimization: OptimizationConfig,
        seed: int,
        optimizer_root: Path,
        campaign_id: UUID | None = None,
    ) -> UUID:
        """Persist a validated campaign document and reproducibility metadata."""

        campaign_id = campaign_id or uuid4()
        runtime = capture_runtime_metadata()
        storage_uri = optimizer_storage_url(optimizer_root, campaign_id, optimization.strategy)
        with self.storage.unit_of_work() as uow:
            uow.campaigns.add(
                campaign_id=str(campaign_id),
                name=name,
                state=CampaignState.DRAFT,
                schema_version=schema_version,
                config_source=raw_text,
                config=config,
                config_sha256=config_sha256,
                config_source_sha256=source_sha256,
                objective=objective,
                parameter_space=parameter_space,
                application_version=runtime.application_version,
                git_commit=runtime.git_commit,
                git_dirty=runtime.git_dirty,
                source_tree_diff_sha256=runtime.source_tree_diff_sha256,
                python_version=runtime.python_version,
                platform=runtime.platform,
                uv_lock_sha256=runtime.uv_lock_sha256,
            )
            transition_campaign_row(uow, campaign_id, CampaignState.DRAFT, CampaignState.READY)
            uow.optimization.add_run(
                optimizer_run_id=str(optimizer_run_id(campaign_id)),
                campaign_id=str(campaign_id),
                strategy_name=optimization.strategy,
                strategy_version="1.0.0",
                seed=seed,
                config=optimization,
                status="ready",
                external_storage_uri=storage_uri,
            )
            uow.commit()
        return campaign_id

    def load(
        self, campaign_id: UUID
    ) -> tuple[CampaignRecord, OptimizerRunRecord, OptimizationState | None]:
        with self.storage.unit_of_work() as uow:
            campaign = uow.campaigns.get(str(campaign_id))
            assert uow.session is not None
            optimizer_run = uow.session.scalar(
                select(OptimizerRunRecord).where(OptimizerRunRecord.campaign_id == str(campaign_id))
            )
            if optimizer_run is None:
                raise CampaignExecutionError("campaign has no optimizer run")
            checkpoint_record = uow.optimization.latest_checkpoint(optimizer_run.id)
            checkpoint = (
                None
                if checkpoint_record is None
                else OptimizationState.model_validate(
                    checkpoint_record.state_snapshot, strict=False
                )
            )
            uow.commit()
            return campaign, optimizer_run, checkpoint

    def checkpoint(self, optimizer_run_id: str, strategy: OptimizationStrategy) -> None:
        state = strategy.checkpoint()
        with self.storage.unit_of_work() as uow:
            uow.optimization.add_checkpoint(
                optimizer_run_id=optimizer_run_id,
                ordinal=next_checkpoint_ordinal(uow, optimizer_run_id, state.suggestion_count),
                state=state,
            )
            uow.commit()

    def finalize_observation(
        self,
        optimizer_run_id: str,
        strategy: OptimizationStrategy,
        experiment_id: UUID,
    ) -> None:
        """Mark a durably queued observation delivered and checkpoint atomically."""

        with self.storage.unit_of_work() as uow:
            transition_experiment_row(
                uow,
                experiment_id,
                ExperimentState.ANALYSED,
                ExperimentState.OBSERVED,
            )
            state = strategy.checkpoint()
            uow.optimization.add_checkpoint(
                optimizer_run_id=optimizer_run_id,
                ordinal=next_checkpoint_ordinal(uow, optimizer_run_id, state.suggestion_count),
                state=state,
            )
            uow.commit()

    def trial_records(
        self,
        campaign_id: UUID,
        *,
        allow_human_pending: bool = False,
    ) -> tuple[TrialRecord, ...]:
        terminal = {
            ExperimentState.REJECTED.value: FeedbackDisposition.REJECTED,
            ExperimentState.FAILED.value: FeedbackDisposition.EXECUTION_FAILED,
            ExperimentState.QC_FAILED.value: FeedbackDisposition.QC_FAILED,
            ExperimentState.ABORTED.value: FeedbackDisposition.EXECUTION_FAILED,
        }
        with self.storage.unit_of_work() as uow:
            assert uow.session is not None
            rows = uow.session.execute(
                select(ProposalRecord, ExperimentRecord)
                .join(ExperimentRecord, ProposalRecord.experiment_id == ExperimentRecord.id)
                .where(ExperimentRecord.campaign_id == str(campaign_id))
                .order_by(ExperimentRecord.ordinal)
            ).all()
            observations = {
                row.experiment_id: row
                for row in uow.session.scalars(
                    select(OptimizerObservationRecord)
                    .join(
                        OptimizerRunRecord,
                        OptimizerObservationRecord.optimizer_run_id == OptimizerRunRecord.id,
                    )
                    .where(OptimizerRunRecord.campaign_id == str(campaign_id))
                )
            }
            uow.commit()
        records: list[TrialRecord] = []
        for proposal_row, experiment in rows:
            proposal = ExperimentProposal.model_validate(proposal_row.snapshot, strict=False)
            if experiment.state in {
                ExperimentState.ANALYSED.value,
                ExperimentState.OBSERVED.value,
            }:
                stored = observations.get(experiment.id)
                if stored is None:
                    raise CampaignExecutionError(
                        f"observed experiment {experiment.id} has no optimizer observation"
                    )
                observation = OptimizationObservation.model_validate(
                    stored.observation_snapshot, strict=False
                )
                feedback = StrategyFeedback.observed(
                    observation,
                    proposal.strategy_trial_id,
                )
            elif experiment.state in terminal:
                feedback = StrategyFeedback.excluded(
                    proposal.strategy_trial_id,
                    terminal[experiment.state],
                    reason=experiment.failure_message or experiment.state,
                )
            elif allow_human_pending and experiment.state in {
                ExperimentState.QUEUED.value,
                ExperimentState.AWAITING_EXECUTION.value,
                ExperimentState.RUNNING.value,
                ExperimentState.AWAITING_MEASUREMENTS.value,
                ExperimentState.MEASURED.value,
            }:
                continue
            else:
                raise CampaignExecutionError(
                    f"experiment {experiment.id} is in non-boundary state {experiment.state!r}"
                )
            records.append(TrialRecord(proposal, feedback))
        return tuple(records)

    def assert_boundary_resume(self, campaign_id: UUID) -> None:
        allowed = {
            ExperimentState.REJECTED.value,
            ExperimentState.FAILED.value,
            ExperimentState.QC_FAILED.value,
            ExperimentState.ANALYSED.value,
            ExperimentState.OBSERVED.value,
        }
        with self.storage.unit_of_work() as uow:
            experiments = uow.experiments.list_for_campaign(str(campaign_id))
            assert uow.session is not None
            analysed_evidence = {
                experiment_id: (
                    observation_id,
                    result_id,
                    qc_eligible,
                    optimizer_eligible,
                )
                for (
                    experiment_id,
                    observation_id,
                    result_id,
                    qc_eligible,
                    optimizer_eligible,
                ) in uow.session.execute(
                    select(
                        ExperimentRecord.id,
                        OptimizerObservationRecord.id,
                        ExperimentResultRecord.id,
                        OptimizerObservationRecord.qc_eligible,
                        ExperimentResultRecord.optimizer_eligible,
                    )
                    .outerjoin(
                        OptimizerObservationRecord,
                        OptimizerObservationRecord.experiment_id == ExperimentRecord.id,
                    )
                    .outerjoin(
                        ExperimentResultRecord,
                        ExperimentResultRecord.experiment_id == ExperimentRecord.id,
                    )
                    .where(
                        ExperimentRecord.campaign_id == str(campaign_id),
                        ExperimentRecord.state == ExperimentState.ANALYSED.value,
                    )
                ).all()
            }
            uow.commit()
        nonterminal = [item.id for item in experiments if item.state not in allowed]
        incomplete_outbox = [
            experiment_id
            for experiment_id, evidence in analysed_evidence.items()
            if evidence[0] is None
            or evidence[1] is None
            or evidence[2] is not True
            or evidence[3] is not True
        ]
        if nonterminal:
            raise CampaignExecutionError(
                "automatic resume is safe only at experiment boundaries; "
                f"nonterminal experiments: {nonterminal}"
            )
        if incomplete_outbox:
            raise CampaignExecutionError(
                "analysed experiments may resume only with complete durable observation "
                f"evidence: {incomplete_outbox}"
            )

    def finalize_pending_observations(
        self,
        campaign_id: UUID,
        optimizer_run_id: str,
        strategy: OptimizationStrategy,
    ) -> None:
        """Acknowledge outbox rows after optimiser reconciliation on resume."""

        with self.storage.unit_of_work() as uow:
            pending = [
                item
                for item in uow.experiments.list_for_campaign(str(campaign_id))
                if item.state == ExperimentState.ANALYSED.value
            ]
            if not pending:
                uow.commit()
                return
            for experiment in pending:
                transition_experiment_row(
                    uow,
                    UUID(experiment.id),
                    ExperimentState.ANALYSED,
                    ExperimentState.OBSERVED,
                    reason="durable optimizer observation reconciled after restart",
                )
            state = strategy.checkpoint()
            uow.optimization.add_checkpoint(
                optimizer_run_id=optimizer_run_id,
                ordinal=next_checkpoint_ordinal(uow, optimizer_run_id, state.suggestion_count),
                state=state,
            )
            uow.commit()

    def start_or_resume(
        self,
        campaign_id: UUID,
        current: CampaignState,
        optimizer_run_id: str,
    ) -> None:
        if current not in {CampaignState.READY, CampaignState.PAUSED, CampaignState.RUNNING}:
            raise CampaignExecutionError(f"campaign cannot run from state {current.value!r}")
        with self.storage.unit_of_work() as uow:
            if current is not CampaignState.RUNNING:
                transition_campaign_row(uow, campaign_id, current, CampaignState.RUNNING)
            optimizer = uow.optimization.get_run(optimizer_run_id)
            optimizer.status = "running"
            optimizer.status_reason = None
            uow.commit()

    def pause(
        self,
        campaign_id: UUID,
        optimizer_run_id: str,
        *,
        reason: str = "operator experiment-boundary pause",
    ) -> None:
        with self.storage.unit_of_work() as uow:
            transition_campaign_row(
                uow,
                campaign_id,
                CampaignState.RUNNING,
                CampaignState.PAUSED,
                reason=reason,
            )
            optimizer = uow.optimization.get_run(optimizer_run_id)
            optimizer.status = "paused"
            optimizer.status_reason = reason
            uow.commit()

    def complete(self, campaign_id: UUID, optimizer_run_id: str) -> None:
        with self.storage.unit_of_work() as uow:
            transition_campaign_row(
                uow,
                campaign_id,
                CampaignState.RUNNING,
                CampaignState.COMPLETED,
                reason="experiment budget exhausted",
            )
            uow.optimization.finish_run(optimizer_run_id)
            uow.commit()

    def experiment_count(self, campaign_id: UUID) -> int:
        with self.storage.unit_of_work() as uow:
            return len(uow.experiments.list_for_campaign(str(campaign_id)))


@dataclass(frozen=True, slots=True)
class CampaignStatus:
    campaign_id: UUID
    name: str
    state: CampaignState
    experiment_count: int
    rejected_count: int
    execution_failed_count: int
    aborted_count: int
    qc_failed_count: int
    valid_observation_count: int
    feasible_count: int
    pareto_frontier: tuple[dict[str, object], ...]


def campaign_status(
    storage: Storage, campaign_id: UUID, objective: ObjectiveConfig
) -> CampaignStatus:
    """Counts by terminal state plus the feasible Pareto frontier of observations."""

    with storage.unit_of_work() as uow:
        campaign = uow.campaigns.get(str(campaign_id))
        experiments = uow.experiments.list_for_campaign(str(campaign_id))
        assert uow.session is not None
        observations = list(
            uow.session.scalars(
                select(OptimizerObservationRecord)
                .join(
                    OptimizerRunRecord,
                    OptimizerObservationRecord.optimizer_run_id == OptimizerRunRecord.id,
                )
                .where(OptimizerRunRecord.campaign_id == str(campaign_id))
            )
        )
        uow.commit()
    state_counts: dict[str, int] = {}
    for experiment in experiments:
        state_counts[experiment.state] = state_counts.get(experiment.state, 0) + 1
    points = tuple(
        ParetoPoint(
            identifier=item.experiment_id,
            objectives=tuple(
                float(item.objective_values[name])
                for name in (metric.name for metric in objective.metrics)
            ),
            feasible=item.feasible,
        )
        for item in observations
    )
    frontier = pareto_front(
        points,
        directions=tuple(metric.direction.value for metric in objective.metrics),
    )
    names = tuple(metric.name for metric in objective.metrics)
    return CampaignStatus(
        campaign_id=campaign_id,
        name=campaign.name,
        state=CampaignState(campaign.state),
        experiment_count=len(experiments),
        rejected_count=state_counts.get(ExperimentState.REJECTED.value, 0),
        execution_failed_count=state_counts.get(ExperimentState.FAILED.value, 0),
        aborted_count=state_counts.get(ExperimentState.ABORTED.value, 0),
        qc_failed_count=state_counts.get(ExperimentState.QC_FAILED.value, 0),
        valid_observation_count=len(observations),
        feasible_count=sum(item.feasible for item in observations),
        pareto_frontier=tuple(
            {
                "experiment_id": point.identifier,
                "objectives": dict(zip(names, point.objectives, strict=True)),
            }
            for point in frontier
        ),
    )
