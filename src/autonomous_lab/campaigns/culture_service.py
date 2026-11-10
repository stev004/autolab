"""Closed-loop culture campaigns: ask -> run one culture -> persist evidence -> tell.

This is the culture counterpart of ``CampaignService``. Both share one
``CampaignLedger``, so campaign creation, resume-at-boundary rules, trial
reconciliation, the optimiser outbox and checkpoints have a single
implementation. What differs is the middle: a culture is compiled and validated
by the culture package, executed on one or more replicate reactors through a
setpoint-accepting controller, and its evidence is written into the same
evidence tables the assay path uses, plus one content-addressed JSON artifact
holding the complete run record.

Durability and long-running state (see ``docs/culture.md``):

- the proposal, protocol, validation report and VALIDATED/QUEUED/RUNNING are
  committed *before* any setpoint is sent;
- during the hold every command, acknowledgement, telemetry sample, reading and
  safe-state result is appended to ``action_events`` as it happens (the run
  journal), so a culture survives AutoLab restarting;
- on the next ``run`` an interrupted experiment is resumed from its journal if
  every reactor confirms it is still holding the acknowledged command, or closed
  safely if not. Only when the controller cannot be reached does it need an
  operator (``reconcile``);
- QC, metrics, the result, later transitions and the optimiser outbox row are
  committed in **one** transaction at the end;
- any execution failure pauses the campaign. Cultures are long, and a failed run
  usually needs a person to look at the reactor before the next one.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select

from autonomous_lab.campaigns.culture_config import CultureCampaignConfig
from autonomous_lab.campaigns.lifecycle import (
    CampaignExecutionError,
    CampaignLedger,
    CampaignStatus,
    build_strategy,
    campaign_status,
    result_id,
    transition_campaign_row,
    transition_experiment_row,
)
from autonomous_lab.campaigns.loader import LoadedCultureCampaign
from autonomous_lab.culture import (
    CultureClock,
    CultureControllerCapabilities,
    CultureControllerRegistry,
    CultureEnvironmentController,
    CultureExperimentRecord,
    CultureExperimentRunner,
    CulturePolicy,
    CultureResumeError,
    CultureValidationResult,
    DispatchHeader,
    EnvironmentTelemetry,
    HoldStarted,
    IntegratedCultureSensor,
    InterruptedRun,
    JournalKind,
    ReplicateEvidence,
    RunEvent,
    SafeStateAcknowledgement,
    SampleReading,
    SetpointAcknowledgement,
    SteppedClock,
    WallClock,
)
from autonomous_lab.culture.compiler import CultureProtocolCompiler
from autonomous_lab.culture.qc import QC_VERSION
from autonomous_lab.domain.base import DomainModel
from autonomous_lab.domain.enums import CampaignState, ExperimentState
from autonomous_lab.domain.experiments import (
    ExperimentProposal,
    ExperimentResult,
    PerformanceConstraintResult,
)
from autonomous_lab.domain.identifiers import utc_now
from autonomous_lab.domain.optimization import OptimizationObservation, OptimizationState
from autonomous_lab.domain.protocols import ValidationReport
from autonomous_lab.domain.quantities import QuantityValue
from autonomous_lab.domain.state_machines import StateTransition
from autonomous_lab.hardware.sensors import ReadOnlySensor, load_sensor_adapter
from autonomous_lab.optimization import (
    FeedbackDisposition,
    OptimizationStrategy,
    StrategyFeedback,
    build_optimization_observation,
)
from autonomous_lab.provenance.hashing import canonical_json
from autonomous_lab.storage import Storage, UnitOfWork
from autonomous_lab.storage.artifacts import ArtifactStore
from autonomous_lab.storage.orm import (
    ActionEventRecord,
    AuditEventRecord,
    CampaignRecord,
    ExecutionRunRecord,
    ExperimentRecord,
    MeasurementRecord,
    OptimizerRunRecord,
    ProposalRecord,
)

VALIDATOR_VERSION = "culture-validation-1"
EVIDENCE_ARTIFACT_KIND = "culture_run_record"
CONTAMINATION_EVENT = "culture.contamination_flagged"
RECONCILED_EVENT = "culture.operator_reconciled"
_PENDING = frozenset(
    {
        ExperimentState.PROPOSED.value,
        ExperimentState.VALIDATED.value,
        ExperimentState.QUEUED.value,
        ExperimentState.RUNNING.value,
    }
)


@dataclass(frozen=True, slots=True)
class CultureRig:
    """The devices one culture campaign drives."""

    controller: CultureEnvironmentController
    sensor: ReadOnlySensor


RigFactory = Callable[[CultureCampaignConfig, CultureClock], Awaitable[CultureRig]]
ClockFactory = Callable[[CultureCampaignConfig], CultureClock]


async def installed_rig(config: CultureCampaignConfig, clock: CultureClock) -> CultureRig:
    """Build the rig from installed adapters (entry points), as the CLI does."""

    controller_config = config.execution.controller
    controller = CultureControllerRegistry().create(
        controller_config.adapter,
        controller_id=controller_config.controller_id,
        settings=controller_config.settings,
        clock=clock,
    )
    sensor_config = config.execution.sensor
    if sensor_config is not None:
        sensor = load_sensor_adapter(
            sensor_config.adapter,
            sensor_id=sensor_config.sensor_id,
            settings=sensor_config.settings,
        )
    elif isinstance(controller, IntegratedCultureSensor):
        sensor = controller.culture_sensor()
    else:
        raise CampaignExecutionError(
            "no sensor configured and the controller has no integrated culture sensor"
        )
    return CultureRig(controller=controller, sensor=sensor)


def default_clock(config: CultureCampaignConfig) -> CultureClock:
    if config.execution.clock == "simulated":
        return SteppedClock(utc_now())
    return WallClock()


class CultureCampaignService:
    """Create, run, resume, reconcile and summarise culture campaigns."""

    def __init__(
        self,
        storage: Storage,
        *,
        policy: CulturePolicy,
        optimizer_root: str | Path = Path("data/optimizer"),
        artifact_root: str | Path = Path("data/artifacts"),
        prefer_gp: bool = True,
        rig_factory: RigFactory | None = None,
        clock_factory: ClockFactory | None = None,
        progress_callback: Callable[[CampaignStatus], None] | None = None,
    ) -> None:
        self.storage = storage
        self.ledger = CampaignLedger(storage)
        self.policy = policy
        self.optimizer_root = Path(optimizer_root).expanduser().resolve()
        self.artifacts = ArtifactStore(artifact_root)
        self.prefer_gp = prefer_gp
        self.rig_factory = rig_factory or installed_rig
        self.clock_factory = clock_factory or default_clock
        self.progress_callback = progress_callback
        self.compiler = CultureProtocolCompiler()

    # ------------------------------------------------------------------ public

    def create(self, loaded: LoadedCultureCampaign, *, campaign_id: UUID | None = None) -> UUID:
        config = loaded.config
        return self.ledger.create_campaign(
            name=config.campaign.name,
            schema_version=config.schema_version,
            raw_text=loaded.raw_text,
            config=config,
            config_sha256=loaded.canonical_sha256,
            source_sha256=loaded.source_sha256,
            objective=config.objective,
            parameter_space=config.parameter_space,
            optimization=config.optimization,
            seed=config.campaign.seed,
            optimizer_root=self.optimizer_root,
            campaign_id=campaign_id,
        )

    async def run(
        self, campaign_id: UUID, *, max_new_experiments: int | None = None
    ) -> CampaignStatus:
        """Recover any interrupted experiment, then run until the budget is spent.

        ``max_new_experiments`` is an operator pause hook; it does not change the
        scientific budget in the campaign document.
        """

        if max_new_experiments is not None and max_new_experiments < 1:
            raise ValueError("max_new_experiments must be positive")
        campaign, optimizer_run, checkpoint = self.ledger.load(campaign_id)
        config = culture_config(campaign)
        if CampaignState(campaign.state) is CampaignState.COMPLETED:
            return self.status(campaign_id)
        self._close_unstarted(campaign_id)
        pending = self._pending(campaign_id)
        strategy = self._strategy(campaign_id, config, optimizer_run, checkpoint)
        records = self.ledger.trial_records(campaign_id, allow_human_pending=pending is not None)
        if pending is None:
            strategy.reconcile(records)
        else:
            strategy.reconcile_pending(records, pending[1])
        self.ledger.finalize_pending_observations(campaign_id, optimizer_run.id, strategy)

        clock = self.clock_factory(config)
        rig = await self.rig_factory(config, clock)
        await self._check_rig(config, clock, rig)
        self.ledger.start_or_resume(campaign_id, CampaignState(campaign.state), optimizer_run.id)
        runner = CultureExperimentRunner(
            controller=rig.controller,
            sensor=rig.sensor,
            clock=clock,
            policy=self.policy,
            qc_config=config.culture_qc,
            compiler=self.compiler,
            on_interrupt=config.execution.on_interrupt,
        )

        if pending is not None:
            experiment, proposal = pending
            keep_going = await self._recover(
                config, optimizer_run.id, strategy, experiment, proposal, runner
            )
            if self.progress_callback is not None:
                self.progress_callback(self.status(campaign_id))
            if not keep_going:
                return self.status(campaign_id)
        self.ledger.assert_boundary_resume(campaign_id)

        started_with = self.ledger.experiment_count(campaign_id)
        while self.ledger.experiment_count(campaign_id) < config.campaign.max_experiments:
            if (
                max_new_experiments is not None
                and self.ledger.experiment_count(campaign_id) - started_with >= max_new_experiments
            ):
                self.ledger.pause(campaign_id, optimizer_run.id)
                return self.status(campaign_id)
            proposal = strategy.ask()
            keep_going = await self._run_experiment(
                config, optimizer_run.id, strategy, proposal, runner
            )
            if self.progress_callback is not None:
                self.progress_callback(self.status(campaign_id))
            if not keep_going:
                return self.status(campaign_id)
        self.ledger.complete(campaign_id, optimizer_run.id)
        return self.status(campaign_id)

    def reconcile(
        self, campaign_id: UUID, experiment_id: UUID, *, operator: str, reason: str
    ) -> None:
        """Operator closes an interrupted culture that cannot be resumed automatically.

        The operator attests that they checked the reactor and it is safe. The
        experiment becomes ABORTED (never learned from) and the campaign can resume.
        """

        if not operator.strip() or not reason.strip():
            raise ValueError("operator and reason are required to reconcile a culture")
        with self.storage.unit_of_work() as uow:
            experiment = uow.experiments.get(str(experiment_id))
            if experiment.campaign_id != str(campaign_id):
                raise CampaignExecutionError("experiment does not belong to this campaign")
            culture_config(uow.campaigns.get(str(campaign_id)))
            state = ExperimentState(experiment.state)
            if state not in {ExperimentState.QUEUED, ExperimentState.RUNNING}:
                raise CampaignExecutionError(
                    f"only an interrupted (queued/running) culture can be reconciled, not {state}"
                )
            transition_experiment_row(
                uow, experiment_id, state, ExperimentState.ABORTED, reason=reason
            )
            assert uow.session is not None
            run = uow.session.scalar(
                select(ExecutionRunRecord).where(
                    ExecutionRunRecord.experiment_id == str(experiment_id)
                )
            )
            if run is not None:
                uow.records.finish_execution(
                    run.id,
                    status="aborted",
                    failure_category="operator_reconciled",
                    failure_message=reason,
                    safe_state_reached=None,
                )
            uow.provenance.append_audit(
                campaign_id=str(campaign_id),
                experiment_id=str(experiment_id),
                entity_type="experiment",
                entity_id=str(experiment_id),
                event_type=RECONCILED_EVENT,
                previous_state=state.value,
                new_state=ExperimentState.ABORTED.value,
                actor=operator,
                reason=reason,
            )
            uow.commit()

    def flag_contamination(
        self, campaign_id: UUID, experiment_id: UUID, *, operator: str, reason: str
    ) -> None:
        """Record contamination discovered after a run (e.g. by microscopy or plating).

        Append-only: the evidence is not rewritten. Reports exclude flagged runs
        from their best results and Pareto front. The optimiser has already seen
        the observation and is not retrained; see ``docs/culture.md``.
        """

        if not operator.strip() or not reason.strip():
            raise ValueError("operator and reason are required to flag contamination")
        with self.storage.unit_of_work() as uow:
            experiment = uow.experiments.get(str(experiment_id))
            if experiment.campaign_id != str(campaign_id):
                raise CampaignExecutionError("experiment does not belong to this campaign")
            culture_config(uow.campaigns.get(str(campaign_id)))
            if experiment.state not in {
                ExperimentState.OBSERVED.value,
                ExperimentState.ANALYSED.value,
                ExperimentState.QC_FAILED.value,
            }:
                raise CampaignExecutionError("only a measured culture can be flagged")
            uow.provenance.append_audit(
                campaign_id=str(campaign_id),
                experiment_id=str(experiment_id),
                entity_type="experiment",
                entity_id=str(experiment_id),
                event_type=CONTAMINATION_EVENT,
                actor=operator,
                reason=reason,
            )
            uow.commit()

    def status(self, campaign_id: UUID) -> CampaignStatus:
        with self.storage.unit_of_work() as uow:
            config = culture_config(uow.campaigns.get(str(campaign_id)))
        return campaign_status(self.storage, campaign_id, config.objective)

    def report(self, campaign_id: UUID) -> dict[str, object]:
        """A concise campaign summary: counts, frontier, flags and evidence origins."""

        status = self.status(campaign_id)
        with self.storage.unit_of_work() as uow:
            campaign = uow.campaigns.get(str(campaign_id))
            config = culture_config(campaign)
            assert uow.session is not None
            provenance = list(
                uow.session.scalars(
                    select(MeasurementRecord.provenance)
                    .join(ExperimentRecord, MeasurementRecord.experiment_id == ExperimentRecord.id)
                    .where(ExperimentRecord.campaign_id == str(campaign_id))
                )
            )
            flags = [
                {"experiment_id": row.experiment_id, "operator": row.actor, "reason": row.reason}
                for row in uow.session.scalars(
                    select(AuditEventRecord).where(
                        AuditEventRecord.campaign_id == str(campaign_id),
                        AuditEventRecord.event_type == CONTAMINATION_EVENT,
                    )
                )
            ]
            uow.commit()
        flagged = {str(item["experiment_id"]) for item in flags}
        simulated = sum(1 for item in provenance if item.get("is_mock") is True)
        return {
            "title": "AutoLab culture campaign results",
            "campaign_id": str(campaign_id),
            "campaign_name": campaign.name,
            "state": status.state.value,
            "experiment_count": status.experiment_count,
            "rejected_experiments": status.rejected_count,
            "execution_failures": status.execution_failed_count,
            "aborted_experiments": status.aborted_count,
            "qc_failures": status.qc_failed_count,
            "valid_observations": status.valid_observation_count,
            "replicates_per_experiment": len(config.culture_design.reactor_ids),
            "pareto_frontier": [
                item for item in status.pareto_frontier if str(item["experiment_id"]) not in flagged
            ],
            "contamination_flags": flags,
            "contamination_note": (
                "flagged runs are excluded from this frontier; the optimiser already "
                "learned from them and is not retrained"
                if flags
                else None
            ),
            "evidence_origins": {
                "controller_adapter": config.execution.controller.adapter,
                "clock": config.execution.clock,
                "od_readings": len(provenance),
                "od_readings_from_simulated_or_mock_sensors": simulated,
            },
            "reproducibility": {
                "application_version": campaign.application_version,
                "git_commit": campaign.git_commit,
                "git_dirty": campaign.git_dirty,
                "campaign_config_sha256": campaign.config_sha256,
                "culture_policy_sha256": self.policy.sha256,
            },
        }

    # ------------------------------------------------------- run internals

    def _strategy(
        self,
        campaign_id: UUID,
        config: CultureCampaignConfig,
        optimizer_run: OptimizerRunRecord,
        checkpoint: OptimizationState | None,
    ) -> OptimizationStrategy:
        return build_strategy(
            campaign_id=campaign_id,
            parameter_space=config.parameter_space,
            objective=config.objective,
            optimization=config.optimization,
            seed=config.campaign.seed,
            optimizer_run=optimizer_run,
            checkpoint=checkpoint,
            prefer_gp=self.prefer_gp,
        )

    async def _check_rig(
        self, config: CultureCampaignConfig, clock: CultureClock, rig: CultureRig
    ) -> None:
        """Refuse configurations that would be unsafe before touching the campaign."""

        manifest = await rig.controller.capability_manifest()
        if manifest.controller_id != config.execution.controller.controller_id:
            raise CampaignExecutionError(
                f"controller reports identity {manifest.controller_id!r}, "
                f"configured {config.execution.controller.controller_id!r}"
            )
        if isinstance(clock, SteppedClock) and not manifest.is_simulated:
            # A simulated clock skips waiting. With real hardware that would
            # sample a culture minutes after it started and call it 24 hours.
            raise CampaignExecutionError(
                "a simulated clock may only drive a controller that declares is_simulated"
            )
        missing = sorted(set(config.culture_design.reactor_ids) - manifest.reactor_ids)
        if missing:
            raise CampaignExecutionError(f"controller has no reactors {missing}")
        problems = campaign_range_problems(config, self.policy, manifest)
        if problems:
            # Fail fast: otherwise every proposal would be rejected one by one.
            raise CampaignExecutionError(
                "campaign does not fit the device/policy: " + "; ".join(problems)
            )

    def _close_unstarted(self, campaign_id: UUID) -> None:
        """Close experiments interrupted before dispatch could begin.

        PROPOSED/VALIDATED experiments never reached the device, so closing them is
        always safe: PROPOSED -> REJECTED, VALIDATED -> QUEUED -> FAILED.
        """

        reason = "AutoLab stopped before the culture was dispatched"
        with self.storage.unit_of_work() as uow:
            for experiment in uow.experiments.list_for_campaign(str(campaign_id)):
                experiment_id = UUID(experiment.id)
                if experiment.state == ExperimentState.PROPOSED.value:
                    transition_experiment_row(
                        uow,
                        experiment_id,
                        ExperimentState.PROPOSED,
                        ExperimentState.REJECTED,
                        reason=reason,
                    )
                elif experiment.state == ExperimentState.VALIDATED.value:
                    transition_experiment_row(
                        uow, experiment_id, ExperimentState.VALIDATED, ExperimentState.QUEUED
                    )
                    transition_experiment_row(
                        uow,
                        experiment_id,
                        ExperimentState.QUEUED,
                        ExperimentState.FAILED,
                        reason=reason,
                        metadata={"failure_category": "interrupted_before_dispatch"},
                    )
            uow.commit()

    def _pending(self, campaign_id: UUID) -> tuple[ExperimentRecord, ExperimentProposal] | None:
        with self.storage.unit_of_work() as uow:
            pending = [
                item
                for item in uow.experiments.list_for_campaign(str(campaign_id))
                if item.state in _PENDING
            ]
            if not pending:
                uow.commit()
                return None
            if len(pending) > 1:
                raise CampaignExecutionError(
                    f"more than one interrupted culture: {[item.id for item in pending]}"
                )
            assert uow.session is not None
            snapshot = uow.session.scalar(
                select(ProposalRecord.snapshot).where(ProposalRecord.experiment_id == pending[0].id)
            )
            uow.commit()
        assert snapshot is not None
        return pending[0], ExperimentProposal.model_validate(snapshot, strict=False)

    async def _recover(
        self,
        config: CultureCampaignConfig,
        optimizer_run_id: str,
        strategy: OptimizationStrategy,
        experiment: ExperimentRecord,
        proposal: ExperimentProposal,
        runner: CultureExperimentRunner,
    ) -> bool:
        interrupted = self._load_interrupted(UUID(experiment.id))
        if interrupted is None:
            # QUEUED, or RUNNING with no journal: no setpoint can have been sent.
            reason = "AutoLab stopped before any setpoint was sent"
            with self.storage.unit_of_work() as uow:
                state = ExperimentState(experiment.state)
                if state is ExperimentState.QUEUED:
                    transition_experiment_row(
                        uow, UUID(experiment.id), state, ExperimentState.RUNNING
                    )
                transition_experiment_row(
                    uow,
                    UUID(experiment.id),
                    ExperimentState.RUNNING,
                    ExperimentState.FAILED,
                    reason=reason,
                    metadata={"failure_category": "interrupted_before_dispatch"},
                )
                uow.commit()
            strategy.tell(
                StrategyFeedback.excluded(
                    proposal.strategy_trial_id, FeedbackDisposition.EXECUTION_FAILED, reason=reason
                )
            )
            self.ledger.checkpoint(optimizer_run_id, strategy)
            return True
        observer = _DurableObserver(self.storage, self.policy)
        observer.report = interrupted.validation_report
        journal = _StorageJournal(
            self.storage, observer, next_sequence=self._next_sequence(interrupted.header.run_id)
        )
        journal.run_id = interrupted.header.run_id
        try:
            record = await runner.resume(proposal, interrupted, observer=observer, journal=journal)
        except CultureResumeError as exc:
            self.ledger.pause(proposal.campaign_id, optimizer_run_id, reason=str(exc))
            raise CampaignExecutionError(str(exc)) from exc
        return self._finish(config, optimizer_run_id, strategy, proposal, record, observer)

    async def _run_experiment(
        self,
        config: CultureCampaignConfig,
        optimizer_run_id: str,
        strategy: OptimizationStrategy,
        proposal: ExperimentProposal,
        runner: CultureExperimentRunner,
    ) -> bool:
        with self.storage.unit_of_work() as uow:
            uow.experiments.add(
                experiment_id=str(proposal.experiment_id),
                campaign_id=str(proposal.campaign_id),
                ordinal=proposal.suggestion_index,
                state=ExperimentState.PROPOSED,
                optimizer_trial_id=proposal.strategy_trial_id,
                parent_experiment_ids=(str(item) for item in proposal.parent_experiment_ids),
                created_at=proposal.created_at,
            )
            uow.records.add_proposal(
                proposal_id=str(proposal.proposal_id),
                experiment_id=str(proposal.experiment_id),
                schema_version=proposal.schema_version,
                proposal=proposal,
                strategy_name=proposal.strategy_name,
                strategy_version=proposal.strategy_version,
                strategy_trial_id=proposal.strategy_trial_id,
                created_at=proposal.created_at,
            )
            uow.commit()
        observer = _DurableObserver(self.storage, self.policy)
        journal = _StorageJournal(self.storage, observer)
        record = await runner.run(
            proposal, config.culture_design, observer=observer, journal=journal
        )
        return self._finish(config, optimizer_run_id, strategy, proposal, record, observer)

    def _finish(
        self,
        config: CultureCampaignConfig,
        optimizer_run_id: str,
        strategy: OptimizationStrategy,
        proposal: ExperimentProposal,
        record: CultureExperimentRecord,
        observer: _DurableObserver,
    ) -> bool:
        """Persist the outcome atomically, then tell the optimiser."""

        artifact_id = self._store_record_artifact(proposal, record)
        observation = None
        if record.disposition == "analysed":
            assert record.metrics is not None
            observation = build_optimization_observation(
                problem=strategy.problem,
                campaign_id=proposal.campaign_id,
                experiment_id=proposal.experiment_id,
                result_id=result_id(proposal.experiment_id),
                metric_values={
                    name: value.value for name, value in record.metrics.objective_values().items()
                },
                measurement_uncertainty=record.metrics.standard_errors(),
            )
        with self.storage.unit_of_work() as uow:
            self._persist_results(uow, config, proposal, record, artifact_id, observation)
            observer.flush(uow)
            if observation is not None:
                # Durable optimiser outbox, committed with the evidence it summarises.
                uow.optimization.add_observation(
                    observation_id=str(observation.observation_id),
                    optimizer_run_id=optimizer_run_id,
                    experiment_id=str(proposal.experiment_id),
                    trial_id=proposal.strategy_trial_id,
                    objective_values=observation.objective_values,
                    constraint_residuals=observation.constraint_residuals,
                    qc_eligible=observation.qc_eligible,
                    feasible=observation.feasible,
                    observation=observation,
                    observed_at=observation.observed_at,
                )
            if record.disposition == "failed":
                _pause_in(
                    uow,
                    proposal.campaign_id,
                    optimizer_run_id,
                    f"culture execution failed: {record.reason}",
                )
            uow.commit()
        if observation is not None:
            strategy.tell(StrategyFeedback.observed(observation, proposal.strategy_trial_id))
            self.ledger.finalize_observation(optimizer_run_id, strategy, proposal.experiment_id)
            return True
        disposition = {
            "rejected": FeedbackDisposition.REJECTED,
            "failed": FeedbackDisposition.EXECUTION_FAILED,
            "qc_failed": FeedbackDisposition.QC_FAILED,
        }[record.disposition]
        strategy.tell(
            StrategyFeedback.excluded(
                proposal.strategy_trial_id, disposition, reason=record.reason or record.disposition
            )
        )
        self.ledger.checkpoint(optimizer_run_id, strategy)
        return record.disposition != "failed"

    def _store_record_artifact(
        self, proposal: ExperimentProposal, record: CultureExperimentRecord
    ) -> str:
        """Write the complete run record as content-addressed JSON and register it."""

        content = canonical_json(record).encode("utf-8")
        stored = self.artifacts.put_bytes(content, media_type="application/json", suffix=".json")
        artifact_id = str(
            uuid5(NAMESPACE_URL, f"autolab:{proposal.experiment_id}:culture-record:{stored.sha256}")
        )
        with self.storage.unit_of_work() as uow:
            uow.provenance.add_artifact(
                artifact_id=artifact_id,
                campaign_id=str(proposal.campaign_id),
                experiment_id=str(proposal.experiment_id),
                kind=EVIDENCE_ARTIFACT_KIND,
                uri=stored.uri,
                sha256=stored.sha256,
                media_type=stored.media_type,
                byte_count=stored.byte_count,
                metadata={"record_sha256": record.sha256, "disposition": record.disposition},
            )
            uow.commit()
        return artifact_id

    def _persist_results(
        self,
        uow: UnitOfWork,
        config: CultureCampaignConfig,
        proposal: ExperimentProposal,
        record: CultureExperimentRecord,
        artifact_id: str,
        observation: OptimizationObservation | None,
    ) -> None:
        """Measurements, QC, metrics and result rows. The journal already holds the events."""

        evidence = record.evidence
        if evidence is None:
            return
        run_id = str(evidence.run_id)
        measurement_ids: list[str] = []
        for replicate in evidence.replicates:
            for reading in replicate.readings:
                measurement = reading.measurement
                if measurement is None:
                    continue
                measurement_ids.append(str(measurement.raw_measurement_id))
                uow.records.add_measurement(
                    measurement_id=str(measurement.raw_measurement_id),
                    experiment_id=str(proposal.experiment_id),
                    run_id=run_id,
                    measurement_type=measurement.measurement_type,
                    sample_role="sample",
                    value=float(measurement.raw_value),
                    unit=measurement.unit,
                    raw_payload=measurement,
                    provenance={
                        "measurement_source": "physical_sensor",
                        "is_mock": measurement.source_evidence.is_mock,
                        "sensor_id": measurement.source_evidence.sensor_id,
                        "calibration_sha256": measurement.source_evidence.calibration_sha256,
                        "expected_measurement_id": reading.expected_measurement_id,
                        "reactor_id": replicate.reactor_id,
                        "replicate_index": replicate.replicate_index,
                    },
                    measured_at=measurement.measured_at,
                    well_id=replicate.reactor_id,
                    replicate_index=replicate.replicate_index,
                )
        failed_run = record.disposition == "failed"
        sent = [item for item in evidence.replicates if item.safe_state is not None]
        uow.records.finish_execution(
            run_id,
            status="failed" if failed_run else "succeeded",
            failure_category="culture_execution_failed" if failed_run else None,
            failure_message=record.reason if failed_run else None,
            safe_state_reached=bool(sent)
            and all(item.safe_state is not None and item.safe_state.reached for item in sent),
            completed_at=evidence.ended_at,
        )
        assert uow.session is not None
        run = uow.session.get(ExecutionRunRecord, run_id)
        assert run is not None
        run.device_snapshot = {**run.device_snapshot, "evidence_artifact_id": artifact_id}
        qc = record.qc_report
        if qc is None:
            return
        qc_run_id = uuid5(NAMESPACE_URL, f"autolab:{proposal.experiment_id}:culture-qc")
        uow.records.add_qc_run(
            qc_run_id=str(qc_run_id),
            experiment_id=str(proposal.experiment_id),
            run_id=run_id,
            status="passed" if qc.passed else "failed",
            eligible_for_optimization=qc.passed,
            algorithm_name="culture_integrity_qc",
            algorithm_version=QC_VERSION,
            checks=[
                {
                    "name": check.code,
                    "status": "passed" if check.passed else "failed",
                    "method": check.code,
                    "flagged_measurement_ids": [],
                    "details": {
                        "reactor_id": replicate.reactor_id,
                        "message": check.message,
                        **check.details,
                    },
                }
                for replicate in qc.replicates
                for check in replicate.checks
            ],
            summary=qc,
            completed_at=evidence.ended_at,
        )
        metric_ids: list[str] = []
        if record.metrics is not None:
            for name, value in record.metrics.objective_values().items():
                summary = record.metrics.summaries[name]
                metric_id = str(uuid5(NAMESPACE_URL, f"autolab:{proposal.experiment_id}:{name}"))
                metric_ids.append(metric_id)
                uow.records.add_metric(
                    metric_id=metric_id,
                    experiment_id=str(proposal.experiment_id),
                    qc_run_id=str(qc_run_id),
                    name=name,
                    value=float(value.value),
                    unit=value.unit,
                    uncertainty=None
                    if summary.standard_error is None
                    else {
                        "standard_error": str(summary.standard_error),
                        "standard_deviation": str(summary.standard_deviation),
                        "n": summary.n,
                    },
                    source_measurement_ids=measurement_ids,
                    algorithm_name="culture_analysis_replicate_mean",
                    algorithm_version="2",
                    details={
                        "replicates": {
                            item.reactor_id: str(item.value(name).value)
                            for item in record.metrics.replicates
                        }
                    },
                )
        constraints: tuple[PerformanceConstraintResult, ...] = ()
        if observation is not None:
            constraints = tuple(
                PerformanceConstraintResult(metric=name, residual=residual, passed=residual <= 0)
                for name, residual in observation.constraint_residuals.items()
            )
            for constraint in config.objective.performance_constraints:
                residual = observation.constraint_residuals[constraint.metric]
                uow.records.add_constraint_result(
                    experiment_id=str(proposal.experiment_id),
                    metric_name=constraint.metric,
                    operator=constraint.operator.value,
                    threshold=float(constraint.threshold),
                    actual=float(record.metrics.objective_values()[constraint.metric].value)
                    if record.metrics is not None
                    else 0.0,
                    residual=float(residual),
                    feasible=residual <= 0,
                )
        result = ExperimentResult(
            result_id=result_id(proposal.experiment_id),
            experiment_id=proposal.experiment_id,
            run_id=evidence.run_id,
            measurement_ids=tuple(UUID(item) for item in measurement_ids),
            qc_result_id=qc_run_id,
            derived_metric_ids=tuple(UUID(item) for item in metric_ids),
            performance_constraints=constraints,
            optimizer_eligible=record.disposition == "analysed",
        )
        uow.records.add_experiment_result(
            result_id=str(result.result_id),
            experiment_id=str(proposal.experiment_id),
            run_id=run_id,
            qc_run_id=str(qc_run_id),
            result=result,
            optimizer_eligible=result.optimizer_eligible,
            schema_version=result.schema_version,
            completed_at=result.completed_at,
        )

    # -------------------------------------------------------- journal reload

    def _next_sequence(self, run_id: UUID) -> int:
        with self.storage.unit_of_work() as uow:
            assert uow.session is not None
            latest = uow.session.scalar(
                select(ActionEventRecord.sequence)
                .where(ActionEventRecord.execution_run_id == str(run_id))
                .order_by(ActionEventRecord.sequence.desc())
                .limit(1)
            )
            uow.commit()
        return 1 if latest is None else latest + 1

    def _load_interrupted(self, experiment_id: UUID) -> InterruptedRun | None:
        """Rebuild an interrupted run from its execution row and journal events."""

        with self.storage.unit_of_work() as uow:
            assert uow.session is not None
            run = uow.session.scalar(
                select(ExecutionRunRecord).where(
                    ExecutionRunRecord.experiment_id == str(experiment_id)
                )
            )
            if run is None:
                uow.commit()
                return None
            snapshot = dict(run.device_snapshot)
            rows = list(
                uow.session.execute(
                    select(
                        ActionEventRecord.event_type,
                        ActionEventRecord.device_id,
                        ActionEventRecord.response_snapshot,
                    )
                    .where(ActionEventRecord.execution_run_id == run.id)
                    .order_by(ActionEventRecord.sequence)
                )
            )
            uow.commit()
        header = DispatchHeader.model_validate(snapshot["dispatch_header"], strict=False)
        report = ValidationReport.model_validate(snapshot["validation_report"], strict=False)
        acks: dict[str, SetpointAcknowledgement] = {}
        telemetry: dict[str, list[EnvironmentTelemetry]] = {}
        readings: dict[str, list[SampleReading]] = {}
        safe: dict[str, SafeStateAcknowledgement] = {}
        stopped: set[str] = set()
        interrupted_hold: set[str] = set()
        sent: set[str] = set()
        events: list[RunEvent] = []
        hold_started: datetime | None = None
        for kind, reactor, payload in rows:
            data: dict[str, Any] = payload or {}
            if kind == "setpoint_command" and reactor:
                sent.add(reactor)
            elif kind == "setpoint_ack" and reactor:
                acks[reactor] = SetpointAcknowledgement.model_validate(data, strict=False)
            elif kind == "hold_started":
                hold_started = HoldStarted.model_validate(data, strict=False).at
            elif kind == "telemetry" and reactor:
                telemetry.setdefault(reactor, []).append(
                    EnvironmentTelemetry.model_validate(data, strict=False)
                )
            elif kind == "reading" and reactor:
                readings.setdefault(reactor, []).append(
                    SampleReading.model_validate(data, strict=False)
                )
            elif kind == "safe_state" and reactor:
                safe[reactor] = SafeStateAcknowledgement.model_validate(data, strict=False)
            elif kind == "event":
                event = RunEvent.model_validate(data, strict=False)
                events.append(event)
                if event.reactor_id and event.code == "protection_tripped":
                    stopped.add(event.reactor_id)
                if event.reactor_id and event.code == "hold_not_confirmed":
                    stopped.add(event.reactor_id)
                    interrupted_hold.add(event.reactor_id)
        replicates = tuple(
            ReplicateEvidence(
                replicate_index=spec.replicate_index,
                reactor_id=spec.hold.reactor_id,
                setpoint_command=command,
                acknowledgement=acks.get(spec.hold.reactor_id),
                telemetry=tuple(telemetry.get(spec.hold.reactor_id, [])),
                readings=tuple(readings.get(spec.hold.reactor_id, [])),
                stopped_early=spec.hold.reactor_id in stopped,
                hold_interrupted=spec.hold.reactor_id in interrupted_hold,
                safe_state=safe.get(spec.hold.reactor_id),
            )
            for spec, command in zip(
                header.receipt.protocol.replicates, header.commands, strict=True
            )
        )
        return InterruptedRun(
            protocol=header.receipt.protocol,
            validation_report=report,
            header=header,
            hold_started_at=hold_started,
            replicates=replicates,
            sent_reactors=frozenset(sent),
            events=tuple(events),
            resumed_at=tuple(event.at for event in events if event.code == "resumed"),
        )


class _DurableObserver:
    """Commit pre-dispatch facts immediately; buffer the rest for one final commit."""

    _IMMEDIATE = frozenset(
        {
            ExperimentState.VALIDATED.value,
            ExperimentState.QUEUED.value,
            ExperimentState.RUNNING.value,
        }
    )

    def __init__(self, storage: Storage, policy: CulturePolicy) -> None:
        self.storage = storage
        self.policy = policy
        self.pending: list[StateTransition] = []
        self.report: ValidationReport | None = None

    def on_validated(
        self, result: CultureValidationResult, capabilities: CultureControllerCapabilities
    ) -> None:
        protocol, report = result.protocol, result.report
        self.report = report
        with self.storage.unit_of_work() as uow:
            uow.records.add_protocol(
                protocol_id=str(protocol.protocol_id),
                experiment_id=str(protocol.experiment_id),
                schema_version=protocol.schema_version,
                protocol=protocol,
                compiler_name=protocol.compiler_name,
                compiler_version=protocol.compiler_version,
                protocol_hash=protocol.protocol_hash,
            )
            uow.records.add_validation_report(
                report_id=str(report.report_id),
                protocol_id=str(protocol.protocol_id),
                status=report.status,
                protocol_hash=report.protocol_hash,
                capability_snapshot=capabilities,
                capability_snapshot_hash=report.capability_snapshot_hash,
                validation_policy=self.policy,
                validation_policy_hash=report.validation_policy_hash,
                validator_version=VALIDATOR_VERSION,
                issues=report.issues,
                validated_at=report.validated_at,
            )
            uow.commit()

    def on_transition(self, transition: StateTransition) -> None:
        if transition.to_state in self._IMMEDIATE and not self.pending:
            with self.storage.unit_of_work() as uow:
                _apply(uow, transition)
                uow.commit()
        else:
            self.pending.append(transition)

    def flush(self, uow: UnitOfWork) -> None:
        for transition in self.pending:
            _apply(uow, transition)
        self.pending.clear()


class _StorageJournal:
    """The run journal: each execution fact is committed as an action event at once."""

    def __init__(
        self, storage: Storage, observer: _DurableObserver, *, next_sequence: int = 1
    ) -> None:
        self.storage = storage
        self.observer = observer
        self.sequence = next_sequence
        self.run_id: UUID | None = None

    def dispatching(self, header: DispatchHeader) -> None:
        assert self.observer.report is not None
        self.run_id = header.run_id
        with self.storage.unit_of_work() as uow:
            uow.records.begin_execution(
                run_id=str(header.run_id),
                experiment_id=str(header.experiment_id),
                protocol_id=str(header.receipt.protocol.protocol_id),
                validation_report_id=str(header.receipt.validation_report_id),
                backend_name="culture_controller",
                backend_version=header.receipt.capability_snapshot_hash[:16],
                device_snapshot={
                    "dispatch_header": header,
                    "validation_report": self.observer.report,
                    "controller_capability_sha256": header.receipt.capability_snapshot_hash,
                    "sensor_capability_sha256": header.sensor_capabilities.sha256,
                },
            )
            uow.commit()

    def record(self, kind: JournalKind, payload: DomainModel, *, reactor_id: str | None) -> None:
        assert self.run_id is not None
        with self.storage.unit_of_work() as uow:
            uow.records.append_action_event(
                run_id=str(self.run_id),
                sequence=self.sequence,
                step_id=kind,
                event_type=kind,
                status="recorded",
                idempotency_key=f"{self.run_id}:{self.sequence}",
                # Only the setpoint command changes the physical world.
                retry_safe=kind != "setpoint_command",
                command={"kind": kind, "reactor_id": reactor_id},
                response=payload,
                device_id=reactor_id,
            )
            uow.commit()
        self.sequence += 1


def _apply(uow: UnitOfWork, transition: StateTransition) -> None:
    target = ExperimentState(transition.to_state)
    transition_experiment_row(
        uow,
        transition.entity_id,
        ExperimentState(transition.from_state),
        target,
        reason=transition.reason,
        metadata=(
            {"failure_category": "culture_execution_failed"}
            if target is ExperimentState.FAILED
            else None
        ),
    )


def campaign_range_problems(
    config: CultureCampaignConfig,
    policy: CulturePolicy,
    manifest: CultureControllerCapabilities,
) -> list[str]:
    """Check the campaign's variables against policy ∩ device before any experiment.

    The effective search range of each variable is the policy limit intersected with
    the device's declared range. Variables (optimised plus fixed) must equal the
    device's controlled variables exactly.
    """

    problems: list[str] = []
    optimised = config.parameter_space.parameters
    fixed = config.culture_design.fixed_setpoints
    declared = set(manifest.controlled_variables)
    used = set(optimised) | set(fixed)
    if used != declared:
        problems.append(
            "variables must equal the device's controlled variables "
            f"(missing {sorted(declared - used)}, unknown {sorted(used - declared)})"
        )
    for name in sorted(used & declared):
        variable = manifest.controlled_variables[name]
        unit = variable.unit
        limits = [("device", variable.range)]
        policy_limit = policy.setpoint_limits.get(name)
        if policy_limit is None:
            problems.append(f"the policy does not allow setting {name!r}")
        else:
            limits.append(("policy", policy_limit))
        if name in optimised:
            parameter = optimised[name]
            low = QuantityValue(value=parameter.minimum, unit=parameter.unit).to(unit).value
            high = QuantityValue(value=parameter.maximum, unit=parameter.unit).to(unit).value
        else:
            low = high = fixed[name].to(unit).value
        for source, limit in limits:
            minimum = limit.minimum.to(unit).value
            maximum = limit.maximum.to(unit).value
            if low < minimum or high > maximum:
                problems.append(
                    f"{name} [{low}, {high}] {unit} is outside the {source} range "
                    f"[{minimum}, {maximum}]"
                )
    return problems


def _pause_in(uow: UnitOfWork, campaign_id: UUID, optimizer_run_id: str, reason: str) -> None:
    """Pause inside the caller's transaction, so evidence and pause commit together."""

    transition_campaign_row(
        uow, campaign_id, CampaignState.RUNNING, CampaignState.PAUSED, reason=reason
    )
    optimizer = uow.optimization.get_run(optimizer_run_id)
    optimizer.status = "paused"
    optimizer.status_reason = reason


def culture_config(campaign: CampaignRecord) -> CultureCampaignConfig:
    """Parse a stored culture campaign, refusing campaigns of another type."""

    if campaign.config_json.get("experiment_type") != "culture":
        raise CampaignExecutionError(
            f"campaign {campaign.id} is not a culture campaign; use `autolab campaign ...`"
        )
    return CultureCampaignConfig.model_validate(campaign.config_json, strict=False)
