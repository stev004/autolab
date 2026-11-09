"""Closed-loop culture campaigns: ask -> run one culture -> persist evidence -> tell.

This is the culture counterpart of ``CampaignService``. The two share the same
ledger (``CampaignLedger``): campaign creation, resume-at-boundary rules, trial
reconciliation, the optimiser outbox and checkpoints are one implementation.
What differs is the middle: a culture is compiled and validated by the culture
package, executed against a setpoint-accepting controller, and its evidence is
written into the same evidence tables the assay path uses, plus one
content-addressed JSON artifact holding the complete run record.

Durability rules (see ``docs/culture.md``):

- the proposal, protocol, validation report and VALIDATED/QUEUED/RUNNING
  transitions are committed *before* any setpoint is sent;
- everything learned from the run (execution run, action events, measurements,
  QC, metrics, result, later transitions and the optimiser outbox row) is
  committed in **one** transaction afterwards;
- a crash during the hold leaves the experiment RUNNING; automatic resume then
  refuses until an operator reconciles it, exactly like the assay path;
- any execution failure pauses the campaign. Cultures are long and a failed
  run usually needs a person to look at the reactor before the next one.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
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
    CultureValidationResult,
    IntegratedCultureSensor,
    SteppedClock,
    WallClock,
)
from autonomous_lab.culture.compiler import CultureProtocolCompiler
from autonomous_lab.culture.qc import QC_VERSION
from autonomous_lab.domain.enums import CampaignState, ExperimentState
from autonomous_lab.domain.experiments import (
    ExperimentProposal,
    ExperimentResult,
    PerformanceConstraintResult,
)
from autonomous_lab.domain.identifiers import utc_now
from autonomous_lab.domain.optimization import OptimizationObservation, OptimizationState
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
    CampaignRecord,
    ExperimentRecord,
    MeasurementRecord,
    OptimizerRunRecord,
)

VALIDATOR_VERSION = "culture-validation-1"
EVIDENCE_ARTIFACT_KIND = "culture_run_record"


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
    """Create, run, pause, resume and summarise culture campaigns."""

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
        """Run or resume at experiment boundaries until the budget is spent.

        ``max_new_experiments`` is an operator pause hook; it does not change the
        scientific budget in the campaign document.
        """

        if max_new_experiments is not None and max_new_experiments < 1:
            raise ValueError("max_new_experiments must be positive")
        campaign, optimizer_run, checkpoint = self.ledger.load(campaign_id)
        config = culture_config(campaign)
        if CampaignState(campaign.state) is CampaignState.COMPLETED:
            return self.status(campaign_id)
        self.ledger.assert_boundary_resume(campaign_id)
        strategy = self._strategy(campaign_id, config, optimizer_run, checkpoint)
        strategy.reconcile(self.ledger.trial_records(campaign_id))
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
        )

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

    def status(self, campaign_id: UUID) -> CampaignStatus:
        with self.storage.unit_of_work() as uow:
            config = culture_config(uow.campaigns.get(str(campaign_id)))
        return campaign_status(self.storage, campaign_id, config.objective)

    def report(self, campaign_id: UUID) -> dict[str, object]:
        """A concise campaign summary: counts, best runs and evidence origins."""

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
            uow.commit()
        simulated = sum(1 for item in provenance if item.get("is_mock") is True)
        return {
            "title": "AutoLab culture campaign results",
            "campaign_id": str(campaign_id),
            "campaign_name": campaign.name,
            "state": status.state.value,
            "experiment_count": status.experiment_count,
            "rejected_experiments": status.rejected_count,
            "execution_failures": status.execution_failed_count,
            "qc_failures": status.qc_failed_count,
            "valid_observations": status.valid_observation_count,
            "pareto_frontier": status.pareto_frontier,
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

    # ---------------------------------------------------------------- internals

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
        if config.culture_design.reactor_id not in manifest.reactor_ids:
            raise CampaignExecutionError(
                f"controller has no reactor {config.culture_design.reactor_id!r}"
            )

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
        record = await runner.run(proposal, config.culture_design, observer=observer)
        artifact_id = self._store_record_artifact(proposal, record)

        if record.disposition == "analysed":
            return self._persist_observed(
                config, optimizer_run_id, strategy, proposal, record, observer, artifact_id
            )
        disposition = {
            "rejected": FeedbackDisposition.REJECTED,
            "failed": FeedbackDisposition.EXECUTION_FAILED,
            "qc_failed": FeedbackDisposition.QC_FAILED,
        }[record.disposition]
        with self.storage.unit_of_work() as uow:
            self._persist_execution(uow, proposal, record, artifact_id)
            observer.flush(uow)
            if record.disposition == "failed":
                reason = f"culture execution failed: {record.reason}"
                _pause_in(uow, proposal.campaign_id, optimizer_run_id, reason)
            uow.commit()
        strategy.tell(
            StrategyFeedback.excluded(
                proposal.strategy_trial_id, disposition, reason=record.reason or record.disposition
            )
        )
        self.ledger.checkpoint(optimizer_run_id, strategy)
        return record.disposition != "failed"

    def _persist_observed(
        self,
        config: CultureCampaignConfig,
        optimizer_run_id: str,
        strategy: OptimizationStrategy,
        proposal: ExperimentProposal,
        record: CultureExperimentRecord,
        observer: _DurableObserver,
        artifact_id: str,
    ) -> bool:
        assert record.metrics is not None and record.evidence is not None
        metric_values = {
            name: value.value for name, value in record.metrics.objective_values().items()
        }
        observation = build_optimization_observation(
            problem=strategy.problem,
            campaign_id=proposal.campaign_id,
            experiment_id=proposal.experiment_id,
            result_id=result_id(proposal.experiment_id),
            metric_values=metric_values,
        )
        with self.storage.unit_of_work() as uow:
            self._persist_execution(uow, proposal, record, artifact_id, observation)
            for constraint in config.objective.performance_constraints:
                residual = observation.constraint_residuals[constraint.metric]
                uow.records.add_constraint_result(
                    experiment_id=str(proposal.experiment_id),
                    metric_name=constraint.metric,
                    operator=constraint.operator.value,
                    threshold=float(constraint.threshold),
                    actual=float(metric_values[constraint.metric]),
                    residual=float(residual),
                    feasible=residual <= 0,
                )
            observer.flush(uow)
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
            uow.commit()
        strategy.tell(StrategyFeedback.observed(observation, proposal.strategy_trial_id))
        self.ledger.finalize_observation(optimizer_run_id, strategy, proposal.experiment_id)
        return True

    def _store_record_artifact(
        self, proposal: ExperimentProposal, record: CultureExperimentRecord
    ) -> str:
        """Write the complete run record as content-addressed JSON and register it."""

        content = canonical_json(record).encode("utf-8")
        stored = self.artifacts.put_bytes(content, media_type="application/json", suffix=".json")
        artifact_id = str(uuid5(NAMESPACE_URL, f"autolab:{proposal.experiment_id}:culture-record"))
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

    def _persist_execution(
        self,
        uow: UnitOfWork,
        proposal: ExperimentProposal,
        record: CultureExperimentRecord,
        artifact_id: str,
        observation: OptimizationObservation | None = None,
    ) -> None:
        """Write the execution run, events, measurements, QC, metrics and result rows."""

        evidence = record.evidence
        if evidence is None or record.protocol is None or record.validation_report is None:
            return
        run_id = str(evidence.run_id)
        safe = evidence.safe_state
        uow.records.begin_execution(
            run_id=run_id,
            experiment_id=str(proposal.experiment_id),
            protocol_id=str(record.protocol.protocol_id),
            validation_report_id=str(record.validation_report.report_id),
            backend_name="culture_controller",
            backend_version=evidence.receipt.capability_snapshot_hash[:16],
            device_snapshot={
                "controller_capability_sha256": evidence.controller_capability_sha256,
                "sensor_capability_sha256": evidence.sensor_capabilities.sha256,
                "evidence_artifact_id": artifact_id,
                "telemetry_samples": len(evidence.telemetry),
            },
            started_at=evidence.hold_started_at,
        )
        uow.records.append_action_event(
            run_id=run_id,
            sequence=1,
            step_id="hold_conditions",
            event_type="setpoint_command",
            status="succeeded"
            if evidence.acknowledgement is not None and evidence.acknowledgement.accepted
            else "failed",
            idempotency_key=str(evidence.setpoint_command.command_id),
            retry_safe=False,
            command=evidence.setpoint_command,
            response=evidence.acknowledgement,
            device_id=evidence.setpoint_command.reactor_id,
            occurred_at=evidence.setpoint_command.issued_at,
        )
        uow.records.append_action_event(
            run_id=run_id,
            sequence=2,
            step_id="safe_state",
            event_type="safe_state_request",
            status="succeeded" if safe is not None and safe.reached else "failed",
            idempotency_key=f"{run_id}:safe-state",
            retry_safe=True,
            command={"reason": None if safe is None else safe.reason},
            response=safe,
            occurred_at=evidence.ended_at,
            details={"events": [event.model_dump(mode="json") for event in evidence.events]},
        )
        measurement_ids: list[str] = []
        for reading in evidence.readings:
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
                },
                measured_at=measurement.measured_at,
                well_id=measurement.sample_id,
            )
        failed_run = record.disposition == "failed"
        uow.records.finish_execution(
            run_id,
            status="failed" if failed_run else "succeeded",
            failure_category="culture_execution_failed" if failed_run else None,
            failure_message=record.reason if failed_run else None,
            safe_state_reached=None if safe is None else safe.reached,
            completed_at=evidence.ended_at,
        )
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
                    "details": {"message": check.message, **check.details},
                }
                for check in qc.checks
            ],
            summary=qc,
            completed_at=evidence.ended_at,
        )
        metric_ids: list[str] = []
        constraints: tuple[PerformanceConstraintResult, ...] = ()
        if record.metrics is not None:
            for name, value in record.metrics.objective_values().items():
                metric_id = str(uuid5(NAMESPACE_URL, f"autolab:{proposal.experiment_id}:{name}"))
                metric_ids.append(metric_id)
                uow.records.add_metric(
                    metric_id=metric_id,
                    experiment_id=str(proposal.experiment_id),
                    qc_run_id=str(qc_run_id),
                    name=name,
                    value=float(value.value),
                    unit=value.unit,
                    source_measurement_ids=measurement_ids,
                    algorithm_name="culture_analysis",
                    algorithm_version="1",
                )
        if observation is not None:
            constraints = tuple(
                PerformanceConstraintResult(metric=name, residual=residual, passed=residual <= 0)
                for name, residual in observation.constraint_residuals.items()
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

    def on_validated(
        self, result: CultureValidationResult, capabilities: CultureControllerCapabilities
    ) -> None:
        protocol, report = result.protocol, result.report
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
