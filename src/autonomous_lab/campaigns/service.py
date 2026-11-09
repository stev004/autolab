"""Persistent closed-loop campaign execution.

The campaign service is the application-layer composition root.  Optimisers only
produce scientific parameters; this service compiles, validates, orchestrates,
performs QC/analysis, and then reports eligible observations back to the strategy.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Protocol, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select

from autonomous_lab.analysis.domain_adapters import (
    derived_metrics_from_analysis,
    readings_from_domain,
)
from autonomous_lab.analysis.metrics import analyze_assay
from autonomous_lab.campaigns.config import CampaignConfig, ValidationPolicyConfig
from autonomous_lab.campaigns.lifecycle import (
    CampaignExecutionError,
    CampaignLedger,
    CampaignStatus,
    build_strategy,
    campaign_status,
)
from autonomous_lab.campaigns.lifecycle import result_id as _result_id
from autonomous_lab.campaigns.lifecycle import transition_campaign_row as _transition_campaign
from autonomous_lab.campaigns.lifecycle import (
    transition_experiment_row as _transition_experiment,
)
from autonomous_lab.campaigns.loader import LoadedCampaign
from autonomous_lab.domain.enums import (
    CampaignState,
    ControlType,
    ExperimentState,
    MeasurementRole,
    QCStatus,
)
from autonomous_lab.domain.experiments import (
    ExperimentProposal,
    ExperimentResult,
    PerformanceConstraintResult,
)
from autonomous_lab.domain.measurements import DerivedMetric, Measurement
from autonomous_lab.domain.optimization import OptimizationObservation, OptimizationState
from autonomous_lab.domain.qc import QCCheckResult, QCResult
from autonomous_lab.domain.quantities import QuantityValue
from autonomous_lab.hardware.commands import ExecutionEvidence
from autonomous_lab.hardware.evidence import EvidenceFinalizer, LinearAssayCostCalculator
from autonomous_lab.hardware.registry import DeviceRegistry
from autonomous_lab.human import (
    DeviationCategory,
    ExecutionMode,
    HumanExecutionBackend,
    HumanExecutionRecord,
    WorklistGenerator,
    human_assay_v1_capability_snapshot,
)
from autonomous_lab.human.service import HumanWorkflowService
from autonomous_lab.measurement_ingestion import ExpectedMeasurement, make_expected_plan
from autonomous_lab.measurement_ingestion.repository import IncompleteMeasurementPlanError
from autonomous_lab.optimization import (
    FeedbackDisposition,
    OptimizationStrategy,
    StrategyFeedback,
    TrialRecord,
    build_optimization_observation,
)
from autonomous_lab.orchestration import (
    LabOrchestrator,
    StorageExecutionJournal,
    ValidationServiceReceiptVerifier,
)
from autonomous_lab.protocols import AssayProtocolCompiler
from autonomous_lab.qc import IntegrityQCConfig, evaluate_integrity_qc
from autonomous_lab.qc.domain_adapters import qc_result_from_integrity_report
from autonomous_lab.qc.engine import QCCheck
from autonomous_lab.storage import Storage, UnitOfWork
from autonomous_lab.storage.orm import (
    AuditEventRecord,
    CampaignRecord,
    DerivedMetricRecord,
    ExecutionRunRecord,
    ExperimentRecord,
    MeasurementRecord,
    OptimizerObservationRecord,
    OptimizerRunRecord,
    ProposalRecord,
)
from autonomous_lab.validation import ProtocolValidationService
from autonomous_lab.validation.rules.parameters import parameter_fingerprint

FailureProvider = Callable[[int, ExperimentProposal], object | None]


class ExecutionBackend(Protocol):
    """Minimum backend surface consumed by deterministic orchestration."""

    registry: DeviceRegistry
    backend_name: str
    backend_version: str
    evidence_finalizer: EvidenceFinalizer


BackendFactory = Callable[[CampaignConfig, ExperimentProposal], Awaitable[ExecutionBackend]]


class CampaignService:
    """Create, run, pause, resume, and summarize a local autonomous campaign."""

    def __init__(
        self,
        storage: Storage,
        *,
        validation_policy: ValidationPolicyConfig,
        optimizer_root: str | Path = Path("data/optimizer"),
        prefer_gp: bool = True,
        failure_provider: FailureProvider | None = None,
        backend_factory: BackendFactory | None = None,
        progress_callback: Callable[[CampaignStatus], None] | None = None,
        worklist_root: str | Path = Path("data/worklists"),
        artifact_root: str | Path = Path("data/artifacts"),
    ) -> None:
        self.storage = storage
        self.ledger = CampaignLedger(storage)
        self.validation_policy = validation_policy
        self.optimizer_root = Path(optimizer_root).expanduser().resolve()
        self.prefer_gp = prefer_gp
        self.failure_provider = failure_provider or _no_failures
        self.backend_factory = backend_factory
        self.progress_callback = progress_callback
        self.human_workflow = HumanWorkflowService(
            storage,
            worklist_root=worklist_root,
            artifact_root=artifact_root,
        )
        self.human_backend = HumanExecutionBackend(WorklistGenerator())
        self.compiler = AssayProtocolCompiler()
        self.validator = ProtocolValidationService()

    def create(self, loaded: LoadedCampaign, *, campaign_id: UUID | None = None) -> UUID:
        """Persist a validated campaign document and reproducibility metadata."""

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
        self,
        campaign_id: UUID,
        *,
        max_new_experiments: int | None = None,
    ) -> CampaignStatus:
        """Run or resume at experiment boundaries.

        ``max_new_experiments`` is an operator pause hook and test seam.  It does
        not change the scientific budget in the campaign document.
        """

        if max_new_experiments is not None and max_new_experiments < 1:
            raise ValueError("max_new_experiments must be positive")
        campaign, config, optimizer_run, checkpoint = self._load_campaign(campaign_id)
        if CampaignState(campaign.state) is CampaignState.COMPLETED:
            return self.status(campaign_id)
        if config.execution.mode != "simulation":
            return await self._run_human_campaign(
                campaign,
                config,
                optimizer_run,
                checkpoint,
                max_new_experiments=max_new_experiments,
            )
        self._assert_boundary_resume(campaign_id)
        strategy = self._build_strategy(
            campaign_id,
            config,
            optimizer_run,
            checkpoint,
        )
        strategy.reconcile(self._trial_records(campaign_id))
        self._finalize_pending_observations(campaign_id, optimizer_run.id, strategy)
        self._start_or_resume(campaign_id, CampaignState(campaign.state), optimizer_run.id)

        completed_before = self._experiment_count(campaign_id)
        while self._experiment_count(campaign_id) < config.campaign.max_experiments:
            if (
                max_new_experiments is not None
                and self._experiment_count(campaign_id) - completed_before >= max_new_experiments
            ):
                self._pause(campaign_id, optimizer_run.id)
                return self.status(campaign_id)
            proposal = strategy.ask()
            continue_campaign = await self._run_experiment(
                config, optimizer_run.id, strategy, proposal
            )
            if self.progress_callback is not None:
                self.progress_callback(self.status(campaign_id))
            if not continue_campaign:
                return self.status(campaign_id)

        self._complete(campaign_id, optimizer_run.id)
        return self.status(campaign_id)

    async def _run_human_campaign(
        self,
        campaign: CampaignRecord,
        config: CampaignConfig,
        optimizer_run: OptimizerRunRecord,
        checkpoint: OptimizationState | None,
        *,
        max_new_experiments: int | None,
    ) -> CampaignStatus:
        """Advance a persistent human loop by at most one external boundary."""

        campaign_id = UUID(campaign.id)
        strategy = self._build_strategy(campaign_id, config, optimizer_run, checkpoint)
        pending = self._human_pending_experiment(campaign_id)
        records = self._trial_records(campaign_id, allow_human_pending=True)
        if pending is None:
            strategy.reconcile(records)
        else:
            strategy.reconcile_pending(records, self._proposal_for_experiment(pending.id))
        self._finalize_pending_observations(campaign_id, optimizer_run.id, strategy)
        self._start_or_resume(campaign_id, CampaignState(campaign.state), optimizer_run.id)

        if pending is not None:
            state = ExperimentState(pending.state)
            if state is ExperimentState.MEASURED:
                self._process_external_measurements(config, optimizer_run.id, strategy, pending)
                pending = None
            elif state in {
                ExperimentState.AWAITING_EXECUTION,
                ExperimentState.RUNNING,
                ExperimentState.AWAITING_MEASUREMENTS,
            }:
                self._pause(campaign_id, optimizer_run.id)
                return self.status(campaign_id)
            else:
                raise CampaignExecutionError(
                    f"human campaign has unsupported pending state {state.value!r}"
                )

        if self._experiment_count(campaign_id) >= config.campaign.max_experiments:
            self._complete(campaign_id, optimizer_run.id)
            return self.status(campaign_id)
        if max_new_experiments is not None and max_new_experiments < 1:
            self._pause(campaign_id, optimizer_run.id)
            return self.status(campaign_id)

        # Rejections can be handled synchronously; the first valid experiment
        # stops at the human boundary and the CLI process may exit.
        while self._experiment_count(campaign_id) < config.campaign.max_experiments:
            proposal = strategy.ask()
            awaiting_human = self._prepare_human_experiment(
                campaign,
                config,
                optimizer_run.id,
                strategy,
                proposal,
            )
            if awaiting_human:
                self._pause(campaign_id, optimizer_run.id)
                return self.status(campaign_id)
        self._complete(campaign_id, optimizer_run.id)
        return self.status(campaign_id)

    def _prepare_human_experiment(
        self,
        campaign: CampaignRecord,
        config: CampaignConfig,
        optimizer_run_id: str,
        strategy: OptimizationStrategy,
        proposal: ExperimentProposal,
    ) -> bool:
        """Compile, validate, and persist one operator-ready experiment."""

        campaign_id = proposal.campaign_id
        with self.storage.unit_of_work() as uow:
            uow.experiments.add(
                experiment_id=str(proposal.experiment_id),
                campaign_id=str(campaign_id),
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

        draft_protocol = self.compiler.compile(proposal, config)
        capabilities = human_assay_v1_capability_snapshot()
        validation = self.validator.validate(
            proposal=proposal,
            protocol=draft_protocol,
            campaign=config,
            policy=self.validation_policy,
            capability_snapshot=capabilities,
            historical_parameter_hashes=self._historical_parameter_hashes(
                campaign_id, proposal.experiment_id
            ),
        )
        with self.storage.unit_of_work() as uow:
            uow.records.add_protocol(
                protocol_id=str(validation.protocol.protocol_id),
                experiment_id=str(proposal.experiment_id),
                schema_version=validation.protocol.schema_version,
                protocol=validation.protocol,
                compiler_name=validation.protocol.compiler_name,
                compiler_version=validation.protocol.compiler_version,
                protocol_hash=validation.protocol.protocol_hash,
            )
            uow.records.add_validation_report(
                report_id=str(validation.report.report_id),
                protocol_id=str(validation.protocol.protocol_id),
                status=validation.report.status,
                protocol_hash=validation.report.protocol_hash,
                capability_snapshot=capabilities,
                capability_snapshot_hash=validation.report.capability_snapshot_hash,
                validation_policy=self.validation_policy,
                validation_policy_hash=validation.report.validation_policy_hash,
                validator_version="1.0.0",
                issues=validation.report.issues,
                validated_at=validation.report.validated_at,
            )
            if validation.executable is None:
                _transition_experiment(
                    uow,
                    proposal.experiment_id,
                    ExperimentState.PROPOSED,
                    ExperimentState.REJECTED,
                    reason="deterministic validation rejected the human worklist",
                )
            else:
                _transition_experiment(
                    uow,
                    proposal.experiment_id,
                    ExperimentState.PROPOSED,
                    ExperimentState.VALIDATED,
                )
                _transition_experiment(
                    uow,
                    proposal.experiment_id,
                    ExperimentState.VALIDATED,
                    ExperimentState.QUEUED,
                )
            uow.commit()

        if validation.executable is None:
            strategy.tell(
                StrategyFeedback.excluded(
                    proposal.strategy_trial_id,
                    FeedbackDisposition.REJECTED,
                    reason=",".join(issue.code for issue in validation.report.issues),
                )
            )
            self._checkpoint(optimizer_run_id, strategy)
            return False

        mode = ExecutionMode(config.execution.mode)
        if mode is ExecutionMode.HUMAN_MANUAL:
            human_mode = ExecutionMode.HUMAN_MANUAL
        elif mode is ExecutionMode.HUMAN_SENSOR:
            human_mode = ExecutionMode.HUMAN_SENSOR
        else:
            raise CampaignExecutionError(f"unsupported human execution mode {mode.value!r}")
        worklist = self.human_backend.prepare(
            validation.executable,
            campaign_id=campaign_id,
            campaign_name=campaign.name,
            execution_mode=human_mode,
            operator_id=config.execution.operator_id,
        )
        self.human_workflow.persist_worklist(worklist)
        plan = _measurement_plan_from_protocol(validation.executable.protocol)
        with self.storage.unit_of_work() as uow:
            uow.measurement_ingestion.add_plan(plan)
            _transition_experiment(
                uow,
                proposal.experiment_id,
                ExperimentState.QUEUED,
                ExperimentState.AWAITING_EXECUTION,
                reason="validated worklist awaits a human operator",
            )
            uow.commit()
        self._checkpoint(optimizer_run_id, strategy)
        return True

    def _human_pending_experiment(self, campaign_id: UUID) -> ExperimentRecord | None:
        pending_states = {
            ExperimentState.QUEUED.value,
            ExperimentState.AWAITING_EXECUTION.value,
            ExperimentState.RUNNING.value,
            ExperimentState.AWAITING_MEASUREMENTS.value,
            ExperimentState.MEASURED.value,
        }
        with self.storage.unit_of_work() as uow:
            pending = [
                item
                for item in uow.experiments.list_for_campaign(str(campaign_id))
                if item.state in pending_states
            ]
            for item in pending:
                assert uow.session is not None
                uow.session.expunge(item)
            uow.commit()
        if len(pending) > 1:
            raise CampaignExecutionError("human campaigns may have only one in-flight experiment")
        return pending[0] if pending else None

    def _proposal_for_experiment(self, experiment_id: str) -> ExperimentProposal:
        with self.storage.unit_of_work() as uow:
            assert uow.session is not None
            snapshot = uow.session.scalar(
                select(ProposalRecord.snapshot).where(
                    ProposalRecord.experiment_id == experiment_id
                )
            )
            uow.commit()
        if snapshot is None:
            raise CampaignExecutionError(
                f"human experiment {experiment_id} has no proposal evidence"
            )
        return ExperimentProposal.model_validate(snapshot, strict=False)

    def status(self, campaign_id: UUID) -> CampaignStatus:
        with self.storage.unit_of_work() as uow:
            objective = _assay_config(uow.campaigns.get(str(campaign_id))).objective
        return campaign_status(self.storage, campaign_id, objective)

    def report(self, campaign_id: UUID) -> dict[str, object]:
        """Return a concise simulation report suitable for CLI rendering."""

        status = self.status(campaign_id)
        with self.storage.unit_of_work() as uow:
            campaign = uow.campaigns.get(str(campaign_id))
            config = _assay_config(campaign)
            assert uow.session is not None
            metric_rows = uow.session.execute(
                select(
                    ExperimentRecord.ordinal,
                    ExperimentRecord.id,
                    DerivedMetricRecord.name,
                    DerivedMetricRecord.value,
                    DerivedMetricRecord.unit,
                )
                .join(
                    DerivedMetricRecord,
                    DerivedMetricRecord.experiment_id == ExperimentRecord.id,
                )
                .where(ExperimentRecord.campaign_id == str(campaign_id))
                .order_by(ExperimentRecord.ordinal, DerivedMetricRecord.name)
            ).all()
            observation_rows = uow.session.execute(
                select(
                    ExperimentRecord.ordinal,
                    ExperimentRecord.id,
                    OptimizerObservationRecord.feasible,
                )
                .join(
                    OptimizerObservationRecord,
                    OptimizerObservationRecord.experiment_id == ExperimentRecord.id,
                )
                .where(ExperimentRecord.campaign_id == str(campaign_id))
                .order_by(ExperimentRecord.ordinal)
            ).all()
            proposal_rows = uow.session.execute(
                select(ExperimentRecord.id, ProposalRecord.snapshot)
                .join(ProposalRecord, ProposalRecord.experiment_id == ExperimentRecord.id)
                .where(ExperimentRecord.campaign_id == str(campaign_id))
            ).all()
            simulator_provenance = uow.session.scalar(
                select(MeasurementRecord.provenance)
                .join(
                    ExperimentRecord,
                    MeasurementRecord.experiment_id == ExperimentRecord.id,
                )
                .where(ExperimentRecord.campaign_id == str(campaign_id))
                .order_by(ExperimentRecord.ordinal, MeasurementRecord.created_at)
                .limit(1)
            )
            execution_runs = list(
                uow.session.execute(
                    select(ExecutionRunRecord.backend_name, ExecutionRunRecord.status)
                    .join(
                        ExperimentRecord,
                        ExecutionRunRecord.experiment_id == ExperimentRecord.id,
                    )
                    .where(ExperimentRecord.campaign_id == str(campaign_id))
                ).all()
            )
            measurement_provenance_rows = list(
                uow.session.scalars(
                    select(MeasurementRecord.provenance)
                    .join(
                        ExperimentRecord,
                        MeasurementRecord.experiment_id == ExperimentRecord.id,
                    )
                    .where(ExperimentRecord.campaign_id == str(campaign_id))
                )
            )
            human_events = list(
                uow.session.scalars(
                    select(AuditEventRecord.details).where(
                        AuditEventRecord.campaign_id == str(campaign_id),
                        AuditEventRecord.event_type == "human.execution_completed",
                    )
                )
            )
            uow.commit()
        by_experiment: dict[str, dict[str, float]] = {}
        metric_units: dict[str, str] = {}
        ordinals: dict[str, int] = {}
        for ordinal, experiment_id, name, value, unit in metric_rows:
            ordinals[experiment_id] = ordinal
            by_experiment.setdefault(experiment_id, {})[name] = value
            metric_units[name] = unit
        proposals = {
            experiment_id: snapshot.get("parameter_values", {})
            for experiment_id, snapshot in proposal_rows
        }
        feasible_ids = {
            experiment_id
            for _ordinal, experiment_id, feasible in observation_rows
            if feasible and experiment_id in by_experiment
        }
        first_feasible = min(feasible_ids, key=lambda item: ordinals[item], default=None)
        initial = (
            None
            if first_feasible is None
            else _solution_summary(
                first_feasible,
                ordinals,
                proposals,
                by_experiment,
                metric_units,
            )
        )
        best_by_objective: dict[str, dict[str, object]] = {}
        for objective in config.objective.metrics:
            candidates = [
                experiment_id
                for experiment_id in feasible_ids
                if objective.name in by_experiment[experiment_id]
            ]
            if not candidates:
                continue
            selector = min if objective.direction.value == "minimize" else max
            best_id = selector(
                candidates,
                key=lambda item: by_experiment[item][objective.name],
            )
            best_by_objective[objective.name] = _solution_summary(
                best_id,
                ordinals,
                proposals,
                by_experiment,
                metric_units,
            )
        frontier_ids = [str(item["experiment_id"]) for item in status.pareto_frontier]
        pareto_recipes = [
            _solution_summary(
                experiment_id,
                ordinals,
                proposals,
                by_experiment,
                metric_units,
            )
            for experiment_id in frontier_ids
            if experiment_id in by_experiment
        ]
        initial_metric_values = None if first_feasible is None else by_experiment[first_feasible]
        best_values = {
            name: by_experiment[str(summary["experiment_id"])][name]
            for name, summary in best_by_objective.items()
        }
        return {
            "title": (
                "AutoLab V0 simulation validation results"
                if config.execution.mode == "simulation"
                else "AutoLab V0.5 human-loop campaign results"
            ),
            "campaign_id": str(campaign_id),
            "campaign_name": campaign.name,
            "state": status.state.value,
            "experiment_count": status.experiment_count,
            "rejected_experiments": status.rejected_count,
            "execution_failures": status.execution_failed_count,
            "aborted_experiments": status.aborted_count,
            "qc_failures": status.qc_failed_count,
            "valid_observations": status.valid_observation_count,
            "feasible_observations": status.feasible_count,
            "initial_feasible_solution": initial,
            "best_feasible_by_objective": best_by_objective,
            "cost_reduction_fraction": _reduction(
                initial_metric_values, best_values, "cost_per_test"
            ),
            "reagent_reduction_fraction": _reduction(
                initial_metric_values, best_values, "reagent_consumption"
            ),
            "pareto_frontier": status.pareto_frontier,
            "pareto_recipes": pareto_recipes,
            "evidence_origins": {
                "execution_mode": config.execution.mode,
                "simulated_device_experiments": sum(
                    backend == "simulated" and run_status == "succeeded"
                    for backend, run_status in execution_runs
                ),
                "human_executed_experiments": sum(
                    backend == "human_operator" and run_status == "succeeded"
                    for backend, run_status in execution_runs
                ),
                "human_aborted_experiments": sum(
                    backend == "human_operator" and run_status == "aborted"
                    for backend, run_status in execution_runs
                ),
                "measurements": _count_measurement_origins(measurement_provenance_rows),
                "protocol_deviations": sum(
                    len(event.get("deviations", [])) for event in human_events
                ),
            },
            "reproducibility": {
                "application_version": campaign.application_version,
                "git_commit": campaign.git_commit,
                "python_version": campaign.python_version,
                "platform": campaign.platform,
                "uv_lock_sha256": campaign.uv_lock_sha256,
                "campaign_config_sha256": campaign.config_sha256,
                "campaign_source_sha256": campaign.config_source_sha256,
                "git_dirty": campaign.git_dirty,
                "source_tree_diff_sha256": campaign.source_tree_diff_sha256,
                "execution_mode": config.execution.mode,
                "simulator_model": (
                    campaign.config_json["simulation"]["model"]
                    if config.execution.mode == "simulation"
                    else None
                ),
                "simulator_seed": (
                    campaign.config_json["simulation"]["seed"]
                    if config.execution.mode == "simulation"
                    else None
                ),
                "simulator_model_version": (
                    None
                    if simulator_provenance is None
                    else simulator_provenance.get("simulator_model_version")
                ),
                "simulator_config_hash": (
                    None
                    if simulator_provenance is None
                    else simulator_provenance.get("simulator_config_hash")
                ),
                "external_cost_accounting": (
                    None
                    if config.execution.mode == "simulation"
                    else "configured linear coefficients; not an instrument measurement"
                ),
            },
        }

    async def _run_experiment(
        self,
        config: CampaignConfig,
        optimizer_run_id: str,
        strategy: OptimizationStrategy,
        proposal: ExperimentProposal,
    ) -> bool:
        campaign_id = proposal.campaign_id
        with self.storage.unit_of_work() as uow:
            uow.experiments.add(
                experiment_id=str(proposal.experiment_id),
                campaign_id=str(campaign_id),
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

        backend = await self._build_backend(config, proposal)
        draft_protocol = self.compiler.compile(proposal, config)
        historical = self._historical_parameter_hashes(campaign_id, proposal.experiment_id)
        validation = self.validator.validate(
            proposal=proposal,
            protocol=draft_protocol,
            campaign=config,
            policy=self.validation_policy,
            capability_snapshot=backend.registry.snapshot,
            historical_parameter_hashes=historical,
        )
        with self.storage.unit_of_work() as uow:
            uow.records.add_protocol(
                protocol_id=str(validation.protocol.protocol_id),
                experiment_id=str(proposal.experiment_id),
                schema_version=validation.protocol.schema_version,
                protocol=validation.protocol,
                compiler_name=validation.protocol.compiler_name,
                compiler_version=validation.protocol.compiler_version,
                protocol_hash=validation.protocol.protocol_hash,
            )
            uow.records.add_validation_report(
                report_id=str(validation.report.report_id),
                protocol_id=str(validation.protocol.protocol_id),
                status=validation.report.status,
                protocol_hash=validation.report.protocol_hash,
                capability_snapshot=backend.registry.snapshot,
                capability_snapshot_hash=validation.report.capability_snapshot_hash,
                validation_policy=self.validation_policy,
                validation_policy_hash=validation.report.validation_policy_hash,
                validator_version="1.0.0",
                issues=validation.report.issues,
                validated_at=validation.report.validated_at,
            )
            if validation.executable is None:
                _transition_experiment(
                    uow,
                    proposal.experiment_id,
                    ExperimentState.PROPOSED,
                    ExperimentState.REJECTED,
                    reason="deterministic validation rejected the protocol",
                )
            else:
                _transition_experiment(
                    uow,
                    proposal.experiment_id,
                    ExperimentState.PROPOSED,
                    ExperimentState.VALIDATED,
                )
                _transition_experiment(
                    uow,
                    proposal.experiment_id,
                    ExperimentState.VALIDATED,
                    ExperimentState.QUEUED,
                )
            uow.commit()

        if validation.executable is None:
            strategy.tell(
                StrategyFeedback.excluded(
                    proposal.strategy_trial_id,
                    FeedbackDisposition.REJECTED,
                    reason=",".join(issue.code for issue in validation.report.issues),
                )
            )
            self._checkpoint(optimizer_run_id, strategy)
            return True

        with self.storage.unit_of_work() as uow:
            _transition_experiment(
                uow,
                proposal.experiment_id,
                ExperimentState.QUEUED,
                ExperimentState.RUNNING,
            )
            uow.commit()
        run_id = uuid5(NAMESPACE_URL, f"autolab:{proposal.experiment_id}:execution:1")
        verifier = ValidationServiceReceiptVerifier.from_reports(
            {validation.report.report_id: validation.report},
            validation_service=self.validator,
            policy=self.validation_policy,
        )
        journal = StorageExecutionJournal(
            self.storage,
            backend_name=backend.backend_name,
            backend_version=backend.backend_version,
        )
        execution = await LabOrchestrator(
            backend.registry,
            verifier,
            journal=journal,
            evidence_finalizer=backend.evidence_finalizer,
        ).execute(validation.executable, run_id=run_id)
        if execution.status == "failed":
            unsafe_failure = (
                execution.safe_state_reached is not True
                or execution.failure_category == "invalid_execution_evidence"
                or any(action.ambiguous for action in execution.actions)
            )
            pause_reason = (
                "execution paused after invalid finalized evidence"
                if execution.failure_category == "invalid_execution_evidence"
                else "execution paused after ambiguous or unconfirmed-safe failure: "
                f"{execution.failure_category or 'execution_failed'}"
            )
            with self.storage.unit_of_work() as uow:
                _transition_experiment(
                    uow,
                    proposal.experiment_id,
                    ExperimentState.RUNNING,
                    ExperimentState.FAILED,
                    reason=execution.failure_message,
                    metadata={"failure_category": execution.failure_category},
                )
                if unsafe_failure:
                    _transition_campaign(
                        uow,
                        campaign_id,
                        CampaignState.RUNNING,
                        CampaignState.PAUSED,
                        reason=pause_reason,
                    )
                    optimizer = uow.optimization.get_run(optimizer_run_id)
                    optimizer.status = "paused"
                    optimizer.status_reason = pause_reason
                uow.commit()
            strategy.tell(
                StrategyFeedback.excluded(
                    proposal.strategy_trial_id,
                    FeedbackDisposition.EXECUTION_FAILED,
                    reason=execution.failure_category or "execution_failed",
                )
            )
            self._checkpoint(optimizer_run_id, strategy)
            return not unsafe_failure

        evidence = execution.execution_evidence
        evidence_error = _campaign_evidence_error(evidence)
        if evidence_error is not None:
            pause_reason = f"execution paused after invalid finalized evidence: {evidence_error}"
            with self.storage.unit_of_work() as uow:
                _transition_experiment(
                    uow,
                    proposal.experiment_id,
                    ExperimentState.RUNNING,
                    ExperimentState.FAILED,
                    reason=evidence_error,
                    metadata={"failure_category": "invalid_execution_evidence"},
                )
                _transition_campaign(
                    uow,
                    campaign_id,
                    CampaignState.RUNNING,
                    CampaignState.PAUSED,
                    reason=pause_reason,
                )
                optimizer = uow.optimization.get_run(optimizer_run_id)
                optimizer.status = "paused"
                optimizer.status_reason = pause_reason
                uow.commit()
            strategy.tell(
                StrategyFeedback.excluded(
                    proposal.strategy_trial_id,
                    FeedbackDisposition.EXECUTION_FAILED,
                    reason="invalid_execution_evidence",
                )
            )
            self._checkpoint(optimizer_run_id, strategy)
            return False

        with self.storage.unit_of_work() as uow:
            _transition_experiment(
                uow,
                proposal.experiment_id,
                ExperimentState.RUNNING,
                ExperimentState.MEASURED,
            )
            uow.commit()

        assert evidence is not None
        evidence_readings = readings_from_domain(execution.measurements)
        qc_report = evaluate_integrity_qc(
            evidence_readings,
            self._qc_config(config),
        )
        measurement_id_map = {
            str(acquisition.acquisition_id): acquisition.acquisition_id
            for acquisition in evidence.acquisitions
        }
        qc_result = qc_result_from_integrity_report(
            qc_report,
            experiment_id=proposal.experiment_id,
            run_id=run_id,
            measurement_id_map=measurement_id_map,
        )
        if qc_result.status is QCStatus.FAILED:
            result = ExperimentResult(
                result_id=_result_id(proposal.experiment_id),
                experiment_id=proposal.experiment_id,
                run_id=run_id,
                measurement_ids=tuple(item.measurement_id for item in execution.measurements),
                qc_result_id=qc_result.qc_result_id,
                optimizer_eligible=False,
            )
            self._persist_qc_result(qc_result, result)
            with self.storage.unit_of_work() as uow:
                _transition_experiment(
                    uow,
                    proposal.experiment_id,
                    ExperimentState.MEASURED,
                    ExperimentState.QC_FAILED,
                    reason=",".join(qc_report.failed_check_codes),
                )
                uow.commit()
            strategy.tell(
                StrategyFeedback.excluded(
                    proposal.strategy_trial_id,
                    FeedbackDisposition.QC_FAILED,
                    reason=",".join(qc_report.failed_check_codes),
                )
            )
            self._checkpoint(optimizer_run_id, strategy)
            return True

        analysis = analyze_assay(
            readings_from_domain(execution.measurements),
            cost_per_test=_evidence_value(evidence.costs, "cost_per_test", "GBP"),
            reagent_consumption_mg_per_test=_evidence_value(
                evidence.costs,
                "reagent_consumption_per_test",
                "milligram",
            ),
            reagent_volume_microliter_per_test=_evidence_value(
                evidence.executed_parameters,
                "reagent_volume",
                "microliter",
            ),
        )
        metrics = derived_metrics_from_analysis(
            analysis,
            experiment_id=proposal.experiment_id,
        )
        metric_values = analysis.metric_values()
        observation = build_optimization_observation(
            problem=strategy.problem,
            campaign_id=campaign_id,
            experiment_id=proposal.experiment_id,
            result_id=_result_id(proposal.experiment_id),
            metric_values=metric_values,
            measurement_uncertainty={
                "response_standard_uncertainty": analysis.response_uncertainty.standard_uncertainty
            },
        )
        performance = tuple(
            PerformanceConstraintResult(
                metric=name,
                residual=residual,
                passed=residual <= 0,
            )
            for name, residual in observation.constraint_residuals.items()
        )
        result = ExperimentResult(
            result_id=observation.result_id,
            experiment_id=proposal.experiment_id,
            run_id=run_id,
            measurement_ids=tuple(item.measurement_id for item in execution.measurements),
            qc_result_id=qc_result.qc_result_id,
            derived_metric_ids=tuple(metric.metric_id for metric in metrics),
            performance_constraints=performance,
            optimizer_eligible=True,
        )
        self._persist_analysis(
            config,
            optimizer_run_id,
            qc_result,
            result,
            metrics,
            observation,
            proposal.strategy_trial_id,
            metric_values,
        )
        strategy.tell(StrategyFeedback.observed(observation, proposal.strategy_trial_id))
        self._finalize_observation(optimizer_run_id, strategy, proposal.experiment_id)
        return True

    def _process_external_measurements(
        self,
        config: CampaignConfig,
        optimizer_run_id: str,
        strategy: OptimizationStrategy,
        experiment: ExperimentRecord,
    ) -> None:
        """QC and analyse one fully reconciled external measurement plan exactly once."""

        experiment_id = UUID(experiment.id)
        run_id = _human_execution_run_id(experiment_id)
        with self.storage.unit_of_work() as uow:
            locked = uow.experiments.get_for_update(experiment.id)
            if locked.state != ExperimentState.MEASURED.value:
                raise CampaignExecutionError("external measurements must be in MEASURED state")
            assert uow.session is not None
            proposal_row = uow.session.scalar(
                select(ProposalRecord).where(ProposalRecord.experiment_id == experiment.id)
            )
            if proposal_row is None:
                raise CampaignExecutionError("human experiment has no proposal evidence")
            proposal = ExperimentProposal.model_validate(proposal_row.snapshot, strict=False)
            existing_rows = list(
                uow.session.scalars(
                    select(MeasurementRecord).where(
                        MeasurementRecord.experiment_id == experiment.id
                    )
                )
            )
            if existing_rows:
                active_ids = {
                    item.id
                    for item in uow.measurement_ingestion.active_revisions(experiment.id)
                    if item.availability == "available"
                }
                if {item.id for item in existing_rows} != active_ids:
                    raise CampaignExecutionError(
                        "frozen measurements differ from active revisions; "
                        "refusing to analyse superseded evidence"
                    )
                measurements = tuple(
                    _domain_measurement_from_record(item) for item in existing_rows
                )
            else:
                try:
                    measurements = uow.measurement_ingestion.active_measurements(
                        experiment.id, str(run_id)
                    )
                except IncompleteMeasurementPlanError as exc:
                    uow.commit()
                    self._fail_incomplete_external_measurements(
                        proposal,
                        run_id,
                        optimizer_run_id,
                        strategy,
                        reason=str(exc),
                    )
                    return
                for item in measurements:
                    source_metadata = item.metadata.get("source_metadata")
                    if not isinstance(source_metadata, dict):
                        source_metadata = {}
                    source_metadata = cast(dict[str, object], source_metadata)
                    sensor_evidence = source_metadata.get("sensor_evidence")
                    if not isinstance(sensor_evidence, dict):
                        sensor_evidence = {}
                    sensor_evidence = cast(dict[str, object], sensor_evidence)
                    uow.records.add_measurement(
                        measurement_id=str(item.measurement_id),
                        experiment_id=str(item.experiment_id),
                        run_id=str(item.run_id),
                        measurement_type=item.measurement_name,
                        sample_role=item.role.value,
                        value=float(item.raw_value),
                        unit=item.unit,
                        raw_payload=item,
                        provenance={
                            "execution_mode": config.execution.mode,
                            "executor_type": "human_operator",
                            "measurement_source": item.metadata.get("measurement_source"),
                            "ingestion_batch_id": item.metadata.get("ingestion_batch_id"),
                            "ingestion_batch_sha256": item.metadata.get(
                                "ingestion_batch_sha256"
                            ),
                            "source_file_sha256": item.metadata.get("source_file_sha256"),
                            "revision_id": item.metadata.get("revision_id"),
                            "revision_number": item.metadata.get("revision_number"),
                            "revision_sha256": item.metadata.get("revision_sha256"),
                            "sensor_id": sensor_evidence.get("sensor_id"),
                            "sensor_capability_sha256": source_metadata.get(
                                "sensor_capability_sha256"
                            ),
                            "calibration_id": sensor_evidence.get("calibration_id"),
                            "calibration_sha256": sensor_evidence.get(
                                "calibration_sha256"
                            ),
                            "is_mock": bool(sensor_evidence.get("is_mock", False)),
                            "fixture_name": source_metadata.get("fixture_name"),
                            "fixture_sha256": source_metadata.get("fixture_sha256"),
                        },
                        measured_at=item.measured_at,
                        well_id=item.vessel_id,
                        analyte_level=(
                            None if item.analyte_level is None else float(item.analyte_level)
                        ),
                        replicate_index=item.replicate_index,
                    )
            execution_record = self._human_execution_record(uow, experiment)
            uow.commit()

        readings = readings_from_domain(measurements)
        qc_report = evaluate_integrity_qc(readings, self._qc_config(config))
        critical = tuple(
            item
            for item in execution_record.deviations
            if item.category
            in {
                DeviationCategory.MISSING_SAMPLE,
                DeviationCategory.CONTAMINATION_SUSPECTED,
                DeviationCategory.OPERATOR_ERROR,
                DeviationCategory.DEVICE_ERROR,
            }
            or (
                item.category
                in {
                    DeviationCategory.QUANTITY_DEVIATION,
                    DeviationCategory.TIMING_DEVIATION,
                }
                and item.vessel_id is not None
            )
        )
        if critical:
            qc_report = replace(
                qc_report,
                passed=False,
                optimizer_eligible=False,
                checks=(
                    *qc_report.checks,
                    QCCheck(
                        code="critical_protocol_deviation",
                        passed=False,
                        message="critical protocol deviations make this run ineligible",
                        details={
                            "deviation_ids": [str(item.deviation_id) for item in critical],
                            "categories": [item.category.value for item in critical],
                        },
                    ),
                ),
            )
        qc_result = qc_result_from_integrity_report(
            qc_report,
            experiment_id=experiment_id,
            run_id=run_id,
        )
        if qc_result.status is QCStatus.FAILED:
            result = ExperimentResult(
                result_id=_result_id(experiment_id),
                experiment_id=experiment_id,
                run_id=run_id,
                measurement_ids=tuple(item.measurement_id for item in measurements),
                qc_result_id=qc_result.qc_result_id,
                optimizer_eligible=False,
            )
            self._persist_qc_result(qc_result, result)
            with self.storage.unit_of_work() as uow:
                _transition_experiment(
                    uow,
                    experiment_id,
                    ExperimentState.MEASURED,
                    ExperimentState.QC_FAILED,
                    reason=",".join(qc_report.failed_check_codes),
                )
                uow.commit()
            strategy.tell(
                StrategyFeedback.excluded(
                    proposal.strategy_trial_id,
                    FeedbackDisposition.QC_FAILED,
                    reason=",".join(qc_report.failed_check_codes),
                )
            )
            self._checkpoint(optimizer_run_id, strategy)
            return

        executed_parameters = _human_executed_parameters(proposal, execution_record)
        cost = config.simulation.cost
        costs = LinearAssayCostCalculator(
            fixed_consumables_per_well=cost.fixed_consumables.to("GBP").value,
            reagent_per_milligram=cost.reagent_per_milligram.to("GBP / milligram").value,
            sample_per_microliter=cost.sample_per_microliter.to("GBP / microliter").value,
            incubation_per_minute=cost.incubation_per_minute.to("GBP / minute").value,
        ).calculate(executed_parameters, executed_well_count=len(measurements))
        analysis = analyze_assay(
            readings,
            cost_per_test=float(costs["cost_per_test"].to("GBP").value),
            reagent_consumption_mg_per_test=float(
                costs["reagent_consumption_per_test"].to("milligram").value
            ),
            reagent_volume_microliter_per_test=float(
                executed_parameters["reagent_volume"].to("microliter").value
            ),
        )
        metrics = derived_metrics_from_analysis(analysis, experiment_id=experiment_id)
        metric_values = analysis.metric_values()
        observation = build_optimization_observation(
            problem=strategy.problem,
            campaign_id=proposal.campaign_id,
            experiment_id=experiment_id,
            result_id=_result_id(experiment_id),
            metric_values=metric_values,
            measurement_uncertainty={
                "response_standard_uncertainty": analysis.response_uncertainty.standard_uncertainty
            },
        )
        result = ExperimentResult(
            result_id=observation.result_id,
            experiment_id=experiment_id,
            run_id=run_id,
            measurement_ids=tuple(item.measurement_id for item in measurements),
            qc_result_id=qc_result.qc_result_id,
            derived_metric_ids=tuple(item.metric_id for item in metrics),
            performance_constraints=tuple(
                PerformanceConstraintResult(
                    metric=name,
                    residual=residual,
                    passed=residual <= 0,
                )
                for name, residual in observation.constraint_residuals.items()
            ),
            optimizer_eligible=True,
        )
        self._persist_analysis(
            config,
            optimizer_run_id,
            qc_result,
            result,
            metrics,
            observation,
            proposal.strategy_trial_id,
            metric_values,
        )
        strategy.tell(StrategyFeedback.observed(observation, proposal.strategy_trial_id))
        self._finalize_observation(optimizer_run_id, strategy, experiment_id)

    def _fail_incomplete_external_measurements(
        self,
        proposal: ExperimentProposal,
        run_id: UUID,
        optimizer_run_id: str,
        strategy: OptimizationStrategy,
        *,
        reason: str,
    ) -> None:
        check = QCCheckResult(
            check_name="measurements_complete",
            check_version="integrity-qc-v1",
            passed=False,
            message=reason,
            flagged_measurement_ids=(),
        )
        qc_result = QCResult(
            experiment_id=proposal.experiment_id,
            run_id=run_id,
            status=QCStatus.FAILED,
            checks=(check,),
        )
        result = ExperimentResult(
            result_id=_result_id(proposal.experiment_id),
            experiment_id=proposal.experiment_id,
            run_id=run_id,
            measurement_ids=(),
            qc_result_id=qc_result.qc_result_id,
            optimizer_eligible=False,
        )
        self._persist_qc_result(qc_result, result)
        with self.storage.unit_of_work() as uow:
            _transition_experiment(
                uow,
                proposal.experiment_id,
                ExperimentState.MEASURED,
                ExperimentState.QC_FAILED,
                reason=reason,
            )
            uow.commit()
        strategy.tell(
            StrategyFeedback.excluded(
                proposal.strategy_trial_id,
                FeedbackDisposition.QC_FAILED,
                reason="measurements_incomplete",
            )
        )
        self._checkpoint(optimizer_run_id, strategy)

    @staticmethod
    def _human_execution_record(
        uow: UnitOfWork,
        experiment: ExperimentRecord,
    ) -> HumanExecutionRecord:
        assert uow.session is not None
        event = uow.session.scalar(
            select(AuditEventRecord)
            .where(
                AuditEventRecord.experiment_id == experiment.id,
                AuditEventRecord.event_type == "human.execution_completed",
            )
            .order_by(AuditEventRecord.occurred_at.desc())
            .limit(1)
        )
        if event is None:
            raise CampaignExecutionError("human experiment has no completion evidence")
        return HumanExecutionRecord.model_validate(event.details, strict=False)

    def _finalize_observation(
        self,
        optimizer_run_id: str,
        strategy: OptimizationStrategy,
        experiment_id: UUID,
    ) -> None:
        self.ledger.finalize_observation(optimizer_run_id, strategy, experiment_id)

    def _persist_qc_result(self, qc_result: QCResult, result: ExperimentResult) -> None:
        with self.storage.unit_of_work() as uow:
            uow.records.add_qc_run(
                qc_run_id=str(qc_result.qc_result_id),
                experiment_id=str(qc_result.experiment_id),
                run_id=str(qc_result.run_id),
                status=qc_result.status,
                eligible_for_optimization=result.optimizer_eligible,
                algorithm_name="integrity_qc",
                algorithm_version="integrity-qc-v1",
                checks=_qc_check_snapshots(qc_result),
                summary=qc_result,
                completed_at=qc_result.evaluated_at,
            )
            uow.records.add_experiment_result(
                result_id=str(result.result_id),
                experiment_id=str(result.experiment_id),
                run_id=str(result.run_id),
                qc_run_id=None if result.qc_result_id is None else str(result.qc_result_id),
                result=result,
                optimizer_eligible=result.optimizer_eligible,
                schema_version=result.schema_version,
                completed_at=result.completed_at,
            )
            uow.commit()

    def _persist_analysis(
        self,
        config: CampaignConfig,
        optimizer_run_id: str,
        qc_result: QCResult,
        result: ExperimentResult,
        metrics: tuple[DerivedMetric, ...],
        observation: OptimizationObservation,
        strategy_trial_id: str,
        metric_values: dict[str, float],
    ) -> None:
        with self.storage.unit_of_work() as uow:
            uow.records.add_qc_run(
                qc_run_id=str(qc_result.qc_result_id),
                experiment_id=str(qc_result.experiment_id),
                run_id=str(qc_result.run_id),
                status=qc_result.status,
                eligible_for_optimization=True,
                algorithm_name="integrity_qc",
                algorithm_version="integrity-qc-v1",
                checks=_qc_check_snapshots(qc_result),
                summary=qc_result,
                completed_at=qc_result.evaluated_at,
            )
            for metric in metrics:
                uow.records.add_metric(
                    metric_id=str(metric.metric_id),
                    experiment_id=str(metric.experiment_id),
                    qc_run_id=str(qc_result.qc_result_id),
                    name=metric.name,
                    value=float(metric.value),
                    unit=metric.unit,
                    uncertainty=(
                        None
                        if metric.uncertainty is None
                        else {"standard_uncertainty": str(metric.uncertainty)}
                    ),
                    source_measurement_ids=(str(item) for item in metric.source_measurement_ids),
                    algorithm_name=metric.algorithm_name,
                    algorithm_version=metric.algorithm_version,
                    details=metric.metadata,
                    created_at=metric.computed_at,
                )
            for constraint in config.objective.performance_constraints:
                residual = observation.constraint_residuals[constraint.metric]
                uow.records.add_constraint_result(
                    experiment_id=str(result.experiment_id),
                    metric_name=constraint.metric,
                    operator=constraint.operator.value,
                    threshold=float(constraint.threshold),
                    actual=metric_values[constraint.metric],
                    residual=float(residual),
                    feasible=residual <= 0,
                )
            uow.records.add_experiment_result(
                result_id=str(result.result_id),
                experiment_id=str(result.experiment_id),
                run_id=str(result.run_id),
                qc_run_id=str(qc_result.qc_result_id),
                result=result,
                optimizer_eligible=True,
                schema_version=result.schema_version,
                completed_at=result.completed_at,
            )
            _transition_experiment(
                uow,
                result.experiment_id,
                ExperimentState.MEASURED,
                ExperimentState.QC_PASSED,
            )
            _transition_experiment(
                uow,
                result.experiment_id,
                ExperimentState.QC_PASSED,
                ExperimentState.ANALYSED,
            )
            # This row is the durable optimisation outbox.  Scientific evidence,
            # eligibility, and the payload to deliver are committed together.
            uow.optimization.add_observation(
                observation_id=str(observation.observation_id),
                optimizer_run_id=optimizer_run_id,
                experiment_id=str(result.experiment_id),
                trial_id=strategy_trial_id,
                objective_values=observation.objective_values,
                constraint_residuals=observation.constraint_residuals,
                qc_eligible=observation.qc_eligible,
                feasible=observation.feasible,
                observation=observation,
                observed_at=observation.observed_at,
            )
            uow.commit()

    def _checkpoint(self, optimizer_run_id: str, strategy: OptimizationStrategy) -> None:
        self.ledger.checkpoint(optimizer_run_id, strategy)

    async def _build_backend(
        self,
        config: CampaignConfig,
        proposal: ExperimentProposal,
    ) -> ExecutionBackend:
        if self.backend_factory is not None:
            return await self.backend_factory(config, proposal)
        return await self._build_simulated_backend(config, proposal)

    async def _build_simulated_backend(
        self,
        config: CampaignConfig,
        proposal: ExperimentProposal,
    ) -> ExecutionBackend:
        if config.execution.mode != "simulation":
            raise CampaignExecutionError(
                f"simulation backend is unavailable in {config.execution.mode!r} mode"
            )
        from autonomous_lab.campaigns.simulation_backend import build_simulated_backend

        # Failure injection remains a simulation-only test seam.
        failures = self.failure_provider(proposal.suggestion_index, proposal)
        return await build_simulated_backend(config, proposal, failures=failures)

    def _qc_config(self, config: CampaignConfig) -> IntegrityQCConfig:
        design = config.assay_design
        return IntegrityQCConfig(
            minimum_blank_replicates=design.blank_replicates,
            minimum_standard_replicates=design.standard_replicates,
            minimum_control_replicates=design.control_replicates,
            minimum_check_standard_replicates=design.check_standard_replicates,
            minimum_distinct_standard_levels=len(design.standard_levels),
        )

    def _build_strategy(
        self,
        campaign_id: UUID,
        config: CampaignConfig,
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

    def _load_campaign(
        self, campaign_id: UUID
    ) -> tuple[CampaignRecord, CampaignConfig, OptimizerRunRecord, OptimizationState | None]:
        campaign, optimizer_run, checkpoint = self.ledger.load(campaign_id)
        return campaign, _assay_config(campaign), optimizer_run, checkpoint

    def _trial_records(
        self,
        campaign_id: UUID,
        *,
        allow_human_pending: bool = False,
    ) -> tuple[TrialRecord, ...]:
        return self.ledger.trial_records(campaign_id, allow_human_pending=allow_human_pending)

    def _assert_boundary_resume(self, campaign_id: UUID) -> None:
        self.ledger.assert_boundary_resume(campaign_id)

    def _finalize_pending_observations(
        self,
        campaign_id: UUID,
        optimizer_run_id: str,
        strategy: OptimizationStrategy,
    ) -> None:
        self.ledger.finalize_pending_observations(campaign_id, optimizer_run_id, strategy)

    def _historical_parameter_hashes(
        self, campaign_id: UUID, current_experiment_id: UUID
    ) -> tuple[str, ...]:
        with self.storage.unit_of_work() as uow:
            assert uow.session is not None
            snapshots = list(
                uow.session.scalars(
                    select(ProposalRecord.snapshot)
                    .join(
                        ExperimentRecord,
                        ProposalRecord.experiment_id == ExperimentRecord.id,
                    )
                    .where(
                        ExperimentRecord.campaign_id == str(campaign_id),
                        ExperimentRecord.id != str(current_experiment_id),
                    )
                )
            )
            campaign = uow.campaigns.get(str(campaign_id))
            parameter_space = _assay_config(campaign).parameter_space
        return tuple(
            parameter_fingerprint(
                ExperimentProposal.model_validate(snapshot, strict=False).parameter_values,
                parameter_space,
            )
            for snapshot in snapshots
        )

    def _start_or_resume(
        self,
        campaign_id: UUID,
        current: CampaignState,
        optimizer_run_id: str,
    ) -> None:
        self.ledger.start_or_resume(campaign_id, current, optimizer_run_id)

    def _pause(
        self,
        campaign_id: UUID,
        optimizer_run_id: str,
        *,
        reason: str = "operator experiment-boundary pause",
    ) -> None:
        self.ledger.pause(campaign_id, optimizer_run_id, reason=reason)

    def _complete(self, campaign_id: UUID, optimizer_run_id: str) -> None:
        self.ledger.complete(campaign_id, optimizer_run_id)

    def _experiment_count(self, campaign_id: UUID) -> int:
        return self.ledger.experiment_count(campaign_id)


def _qc_check_snapshots(result: QCResult) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "name": check.check_name,
            "status": "passed" if check.passed else "failed",
            "method": check.check_name,
            "flagged_measurement_ids": [str(item) for item in check.flagged_measurement_ids],
            "details": {
                "check_version": check.check_version,
                "message": check.message,
                **check.details,
            },
        }
        for check in result.checks
    )


def _campaign_evidence_error(evidence: ExecutionEvidence | None) -> str | None:
    if evidence is None:
        return "successful assay execution produced no finalized evidence artifact"
    if not evidence.fragment_ids or not evidence.acquisitions:
        return "finalized execution evidence is incomplete"
    required = (
        (evidence.costs, "cost_per_test", "GBP"),
        (evidence.costs, "reagent_consumption_per_test", "milligram"),
        (evidence.executed_parameters, "reagent_volume", "microliter"),
    )
    try:
        for values, name, unit in required:
            _evidence_value(values, name, unit)
    except CampaignExecutionError as exc:
        return str(exc)
    return None


def _evidence_value(
    values: dict[str, QuantityValue],
    name: str,
    unit: str,
) -> float:
    try:
        value = values[name]
    except KeyError as exc:
        raise CampaignExecutionError(f"execution evidence is missing {name!r}") from exc
    try:
        return float(value.to(unit).value)
    except ValueError as exc:
        raise CampaignExecutionError(
            f"execution evidence {name!r} is not compatible with {unit!r}"
        ) from exc


def _solution_summary(
    experiment_id: str,
    ordinals: dict[str, int],
    proposals: dict[str, object],
    metrics: dict[str, dict[str, float]],
    metric_units: dict[str, str],
) -> dict[str, object]:
    return {
        "experiment_id": experiment_id,
        "ordinal": ordinals[experiment_id],
        "parameters": proposals.get(experiment_id, {}),
        "metrics": {
            name: {"value": value, "unit": metric_units[name]}
            for name, value in sorted(metrics[experiment_id].items())
        },
    }


def _reduction(
    initial: dict[str, float] | None,
    best: dict[str, float],
    metric: str,
) -> float | None:
    if initial is None or metric not in initial or metric not in best or initial[metric] == 0:
        return None
    return (initial[metric] - best[metric]) / initial[metric]


def _measurement_plan_from_protocol(protocol: object):
    """Bind every expected raw reading to its exact validated vessel and step."""

    from autonomous_lab.domain.protocols import ExperimentProtocol, MeasureAbsorbanceCommand

    typed = ExperimentProtocol.model_validate(protocol, strict=False)
    if typed.protocol_hash is None:
        raise CampaignExecutionError("validated protocol has no content hash")
    measure_steps = [
        step for step in typed.steps if isinstance(step.command, MeasureAbsorbanceCommand)
    ]
    if len(measure_steps) != 1:
        raise CampaignExecutionError("V0.5 human assay requires one measurement step")
    step = measure_steps[0]
    role_by_vessel: dict[str, tuple[MeasurementRole, Decimal | None, int]] = {}
    role_map = {
        ControlType.BLANK: MeasurementRole.BLANK,
        ControlType.STANDARD: MeasurementRole.STANDARD,
        ControlType.POSITIVE: MeasurementRole.CONTROL,
        ControlType.CHECK_STANDARD: MeasurementRole.CHECK_STANDARD,
    }
    for control in typed.controls:
        for replicate_index, vessel_id in enumerate(control.vessel_ids):
            role_by_vessel[vessel_id] = (
                role_map[control.control_type],
                control.nominal_level,
                replicate_index,
            )
    entries = tuple(
        ExpectedMeasurement(
            expected_measurement_id=f"{step.step_id}:{vessel_id}",
            sample_id=vessel_id,
            vessel_id=vessel_id,
            step_id=step.step_id,
            sample_role=role_by_vessel[vessel_id][0],
            measurement_type=typed.measurement_plan.measurement_name,
            unit="absorbance_unit",
            wavelength=typed.measurement_plan.wavelength,
            analyte_level=role_by_vessel[vessel_id][1],
            replicate_index=role_by_vessel[vessel_id][2],
            metadata={
                "location": next(
                    (
                        vessel.location
                        for vessel in typed.vessels
                        if vessel.vessel_id == vessel_id
                    ),
                    None,
                )
            },
        )
        for vessel_id in typed.measurement_plan.vessel_ids
    )
    return make_expected_plan(
        experiment_id=typed.experiment_id,
        protocol_id=typed.protocol_id,
        protocol_hash=typed.protocol_hash,
        entries=entries,
        plan_id=uuid5(NAMESPACE_URL, f"autolab:{typed.protocol_id}:measurement-plan:1"),
        created_at=typed.created_at,
    )


def _human_execution_run_id(experiment_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"autolab:{experiment_id}:human-execution:1")


def _human_executed_parameters(
    proposal: ExperimentProposal,
    record: HumanExecutionRecord,
) -> dict[str, QuantityValue]:
    """Prefer explicit actual batch quantities while retaining requested values."""

    values = {name: quantity.canonical() for name, quantity in proposal.parameter_values.items()}
    for acknowledgement in record.acknowledgements:
        for name, actual in acknowledgement.actual_quantities.items():
            if name in values and actual.is_compatible_with(values[name]):
                values[name] = actual.to(values[name].unit)
    for deviation in record.deviations:
        actual = deviation.actual_quantity
        if actual is None or deviation.vessel_id is not None:
            continue
        parameter_name: str | None = None
        if deviation.category is DeviationCategory.TIMING_DEVIATION:
            parameter_name = "incubation_time"
        elif deviation.category is DeviationCategory.QUANTITY_DEVIATION:
            parameter_name = {
                "reagent": "reagent_volume",
                "sample": "sample_volume",
            }.get(deviation.material_id or "")
        if parameter_name is not None and actual.is_compatible_with(values[parameter_name]):
            values[parameter_name] = actual.to(values[parameter_name].unit)
    return values


def _domain_measurement_from_record(record: MeasurementRecord) -> Measurement:
    """Rehydrate already-persisted external evidence after a process interruption."""

    return Measurement.model_validate(record.raw_payload, strict=False)


def _count_measurement_origins(
    rows: list[dict[str, object]],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for provenance in rows:
        raw_source = provenance.get("measurement_source")
        source = (
            "mock_physical_sensor"
            if raw_source == "physical_sensor" and provenance.get("is_mock") is True
            else str(
                raw_source
                or (
                    "simulated"
                    if provenance.get("simulator_model_version")
                    else "unknown"
                )
            )
        )
        counts[source] = counts.get(source, 0) + 1
    return dict(sorted(counts.items()))


def _no_failures(_ordinal: int, _proposal: ExperimentProposal) -> None:
    return None


def _assay_config(campaign: CampaignRecord) -> CampaignConfig:
    """Parse a stored assay campaign, refusing campaigns of another experiment type."""

    experiment_type = campaign.config_json.get("experiment_type", "assay")
    if experiment_type != "assay":
        raise CampaignExecutionError(
            f"campaign {campaign.id} is a {experiment_type!r} campaign; "
            "use the matching campaign service (for culture: `autolab culture ...`)"
        )
    return CampaignConfig.model_validate(campaign.config_json, strict=False)
