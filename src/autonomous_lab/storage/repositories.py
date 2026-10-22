"""Repository API for durable campaigns and scientific evidence."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Integer, func, select
from sqlalchemy.orm import Session

from autonomous_lab.domain.enums import CampaignState, ExperimentState
from autonomous_lab.domain.state_machines import transition_campaign, transition_experiment
from autonomous_lab.storage._serialization import enum_value, jsonable, snapshot_hash
from autonomous_lab.storage.orm import (
    ActionEventRecord,
    ArtifactRecord,
    AuditEventRecord,
    CampaignRecord,
    DerivedMetricRecord,
    ExecutionRunRecord,
    ExpectedMeasurementPlanRecord,
    ExperimentLineageRecord,
    ExperimentRecord,
    ExperimentResultRecord,
    ExternalMeasurementRevisionRecord,
    MeasurementIngestionBatchRecord,
    MeasurementRecord,
    ObjectiveSnapshotRecord,
    OptimizerCheckpointRecord,
    OptimizerObservationRecord,
    OptimizerRunRecord,
    ParameterSpaceSnapshotRecord,
    PerformanceConstraintRecord,
    ProposalRecord,
    ProtocolRecord,
    QCCheckRecord,
    QCRunRecord,
    ValidationIssueRecord,
    ValidationReportRecord,
    utc_now,
    uuid_string,
)


class RecordNotFoundError(LookupError):
    pass


class StateConflictError(RuntimeError):
    """An optimistic state transition found a different persisted state."""


def _required[ModelT](session: Session, model: type[ModelT], identifier: Any) -> ModelT:
    record = session.get(model, identifier)
    if record is None:
        raise RecordNotFoundError(f"{model.__name__} {identifier!r} does not exist")
    return record


def _now(value: datetime | None) -> datetime:
    return value or utc_now()


def _state_value(enum_type: type[Enum], value: Any) -> str:
    """Accept either a stable enum value or its legacy/member-name spelling."""

    raw = enum_value(value)
    try:
        return str(enum_type(raw).value)
    except ValueError:
        member = enum_type.__members__.get(raw)
        if member is None:
            raise ValueError(
                f"{raw!r} is not a valid {enum_type.__name__}"
            ) from None
        return str(member.value)


def _expected_states(
    enum_type: type[Enum], value: str | Sequence[str] | None
) -> set[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return {_state_value(enum_type, value)}
    return {_state_value(enum_type, item) for item in value}


@dataclass(frozen=True)
class CampaignSummary:
    campaign_id: str
    state: str
    experiment_count: int
    state_counts: Mapping[str, int]
    qc_passed_count: int
    qc_failed_count: int
    observed_count: int
    feasible_count: int


class CampaignRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(
        self,
        *,
        campaign_id: str,
        name: str,
        state: str,
        schema_version: str,
        config_source: str,
        config: Any,
        objective: Any,
        parameter_space: Any,
        config_sha256: str | None = None,
        config_source_sha256: str | None = None,
        application_version: str | None = None,
        git_commit: str | None = None,
        git_dirty: bool | None = None,
        source_tree_diff_sha256: str | None = None,
        python_version: str | None = None,
        platform: str | None = None,
        uv_lock_sha256: str | None = None,
        created_at: datetime | None = None,
    ) -> CampaignRecord:
        timestamp = _now(created_at)
        config_snapshot = jsonable(config)
        objective_snapshot = jsonable(objective)
        parameter_snapshot = jsonable(parameter_space)
        record = CampaignRecord(
            id=str(campaign_id),
            name=name,
            state=_state_value(CampaignState, state),
            schema_version=schema_version,
            config_source=config_source,
            config_json=config_snapshot,
            config_source_sha256=config_source_sha256
            or hashlib.sha256(config_source.encode("utf-8")).hexdigest(),
            config_sha256=config_sha256 or snapshot_hash(config_snapshot),
            application_version=application_version,
            git_commit=git_commit,
            git_dirty=git_dirty,
            source_tree_diff_sha256=source_tree_diff_sha256,
            python_version=python_version,
            platform=platform,
            uv_lock_sha256=uv_lock_sha256,
            created_at=timestamp,
            updated_at=timestamp,
        )
        self.session.add(record)
        # Raw FK values do not give SQLAlchemy an ORM relationship for flush
        # ordering, so persist the parent before appending dependent evidence.
        self.session.flush()
        self.session.add_all(
            [
                ObjectiveSnapshotRecord(
                    campaign_id=record.id,
                    schema_version=schema_version,
                    snapshot=objective_snapshot,
                    sha256=snapshot_hash(objective_snapshot),
                    created_at=timestamp,
                ),
                ParameterSpaceSnapshotRecord(
                    campaign_id=record.id,
                    schema_version=schema_version,
                    snapshot=parameter_snapshot,
                    sha256=snapshot_hash(parameter_snapshot),
                    created_at=timestamp,
                ),
                AuditEventRecord(
                    campaign_id=record.id,
                    entity_type="campaign",
                    entity_id=record.id,
                    event_type="campaign.created",
                    new_state=record.state,
                    occurred_at=timestamp,
                ),
            ]
        )
        return record

    def get(self, campaign_id: str) -> CampaignRecord:
        return _required(self.session, CampaignRecord, str(campaign_id))

    def transition(
        self,
        campaign_id: str,
        new_state: str,
        *,
        expected_state: str | Sequence[str] | None = None,
        actor: str = "system",
        reason: str | None = None,
        details: Any = None,
        occurred_at: datetime | None = None,
    ) -> CampaignRecord:
        record = self.get(campaign_id)
        expected = _expected_states(CampaignState, expected_state)
        if expected is not None and record.state not in expected:
            raise StateConflictError(
                f"campaign {campaign_id} is {record.state!r}, expected one of {sorted(expected)!r}"
            )
        target = _state_value(CampaignState, new_state)
        previous = record.state
        if previous == target:
            return record
        transition_campaign(
            UUID(record.id),
            CampaignState(previous),
            CampaignState(target),
            reason=reason,
        )
        timestamp = _now(occurred_at)
        record.state = target
        record.updated_at = timestamp
        if target.lower() == "running" and record.started_at is None:
            record.started_at = timestamp
        if target.lower() in {"completed", "failed"}:
            record.completed_at = timestamp
        self.session.add(
            AuditEventRecord(
                campaign_id=record.id,
                entity_type="campaign",
                entity_id=record.id,
                event_type="campaign.state_changed",
                previous_state=previous,
                new_state=target,
                actor=actor,
                reason=reason,
                details=jsonable(details or {}),
                occurred_at=timestamp,
            )
        )
        return record

    def summary(self, campaign_id: str) -> CampaignSummary:
        campaign = self.get(campaign_id)
        experiment_states = self.session.execute(
            select(ExperimentRecord.state, func.count(ExperimentRecord.id))
            .where(ExperimentRecord.campaign_id == campaign.id)
            .group_by(ExperimentRecord.state)
        ).all()
        state_counts = {state: count for state, count in experiment_states}
        qc_rows = self.session.execute(
            select(QCRunRecord.status, func.count(QCRunRecord.id))
            .join(ExperimentRecord, QCRunRecord.experiment_id == ExperimentRecord.id)
            .where(ExperimentRecord.campaign_id == campaign.id)
            .group_by(QCRunRecord.status)
        ).all()
        qc_counts: dict[str, int] = {}
        for status, count in qc_rows:
            normalized = status.lower()
            qc_counts[normalized] = qc_counts.get(normalized, 0) + count
        observed_count, feasible_count = self.session.execute(
            select(
                func.count(OptimizerObservationRecord.id),
                func.coalesce(
                    func.sum(func.cast(OptimizerObservationRecord.feasible, Integer)),
                    0,
                ),
            )
            .join(
                OptimizerRunRecord,
                OptimizerObservationRecord.optimizer_run_id == OptimizerRunRecord.id,
            )
            .where(OptimizerRunRecord.campaign_id == campaign.id)
        ).one()
        return CampaignSummary(
            campaign_id=campaign.id,
            state=campaign.state,
            experiment_count=sum(state_counts.values()),
            state_counts=state_counts,
            qc_passed_count=qc_counts.get("passed", 0),
            qc_failed_count=qc_counts.get("failed", 0),
            observed_count=int(observed_count),
            feasible_count=int(feasible_count),
        )


class ExperimentRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(
        self,
        *,
        experiment_id: str,
        campaign_id: str,
        ordinal: int,
        state: str,
        optimizer_trial_id: str | None = None,
        parent_experiment_ids: Iterable[str] = (),
        created_at: datetime | None = None,
    ) -> ExperimentRecord:
        timestamp = _now(created_at)
        record = ExperimentRecord(
            id=str(experiment_id),
            campaign_id=str(campaign_id),
            ordinal=ordinal,
            state=_state_value(ExperimentState, state),
            optimizer_trial_id=optimizer_trial_id,
            created_at=timestamp,
            updated_at=timestamp,
        )
        self.session.add(record)
        self.session.flush()
        self.session.add_all(
            ExperimentLineageRecord(
                experiment_id=record.id,
                parent_experiment_id=str(parent),
                created_at=timestamp,
            )
            for parent in parent_experiment_ids
        )
        self.session.add(
            AuditEventRecord(
                campaign_id=record.campaign_id,
                experiment_id=record.id,
                entity_type="experiment",
                entity_id=record.id,
                event_type="experiment.created",
                new_state=record.state,
                occurred_at=timestamp,
            )
        )
        return record

    def get(self, experiment_id: str) -> ExperimentRecord:
        return _required(self.session, ExperimentRecord, str(experiment_id))

    def list_for_campaign(self, campaign_id: str) -> list[ExperimentRecord]:
        return list(
            self.session.scalars(
                select(ExperimentRecord)
                .where(ExperimentRecord.campaign_id == str(campaign_id))
                .order_by(ExperimentRecord.ordinal)
            )
        )

    def transition(
        self,
        experiment_id: str,
        new_state: str,
        *,
        expected_state: str | Sequence[str] | None = None,
        actor: str = "system",
        reason: str | None = None,
        details: Any = None,
        failure_category: str | None = None,
        failure_message: str | None = None,
        occurred_at: datetime | None = None,
    ) -> ExperimentRecord:
        record = self.get(experiment_id)
        expected = _expected_states(ExperimentState, expected_state)
        if expected is not None and record.state not in expected:
            raise StateConflictError(
                f"experiment {experiment_id} is {record.state!r}, "
                f"expected one of {sorted(expected)!r}"
            )
        target = _state_value(ExperimentState, new_state)
        previous = record.state
        if previous == target:
            return record
        transition_experiment(
            UUID(record.id),
            ExperimentState(previous),
            ExperimentState(target),
            reason=reason,
        )
        timestamp = _now(occurred_at)
        record.state = target
        record.updated_at = timestamp
        record.failure_category = failure_category
        record.failure_message = failure_message
        if target.lower() in {"rejected", "failed", "aborted", "qc_failed", "observed"}:
            record.completed_at = timestamp
        self.session.add(
            AuditEventRecord(
                campaign_id=record.campaign_id,
                experiment_id=record.id,
                entity_type="experiment",
                entity_id=record.id,
                event_type="experiment.state_changed",
                previous_state=previous,
                new_state=target,
                actor=actor,
                reason=reason,
                details=jsonable(details or {}),
                occurred_at=timestamp,
            )
        )
        return record


class ScientificRecordRepository:
    """Append the immutable proposal-to-analysis evidence chain."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def add_proposal(
        self,
        *,
        proposal_id: str,
        experiment_id: str,
        schema_version: str,
        proposal: Any,
        strategy_name: str,
        strategy_version: str | None = None,
        strategy_trial_id: str | None = None,
        created_at: datetime | None = None,
    ) -> ProposalRecord:
        snapshot = jsonable(proposal)
        record = ProposalRecord(
            id=str(proposal_id),
            experiment_id=str(experiment_id),
            schema_version=schema_version,
            snapshot=snapshot,
            sha256=snapshot_hash(snapshot),
            strategy_name=strategy_name,
            strategy_version=strategy_version,
            strategy_trial_id=strategy_trial_id,
            created_at=_now(created_at),
        )
        self.session.add(record)
        return record

    def add_protocol(
        self,
        *,
        protocol_id: str,
        experiment_id: str,
        schema_version: str,
        protocol: Any,
        compiler_name: str,
        compiler_version: str,
        protocol_hash: str | None = None,
        version: int = 1,
        created_at: datetime | None = None,
    ) -> ProtocolRecord:
        snapshot = jsonable(protocol)
        record = ProtocolRecord(
            id=str(protocol_id),
            experiment_id=str(experiment_id),
            version=version,
            schema_version=schema_version,
            snapshot=snapshot,
            protocol_hash=protocol_hash or snapshot_hash(snapshot),
            compiler_name=compiler_name,
            compiler_version=compiler_version,
            created_at=_now(created_at),
        )
        self.session.add(record)
        self.session.flush()
        return record

    def add_validation_report(
        self,
        *,
        report_id: str,
        protocol_id: str,
        status: str,
        protocol_hash: str,
        capability_snapshot: Any,
        validation_policy: Any,
        validator_version: str,
        issues: Iterable[Any] = (),
        capability_snapshot_hash: str | None = None,
        validation_policy_hash: str | None = None,
        validated_at: datetime | None = None,
    ) -> ValidationReportRecord:
        capability = jsonable(capability_snapshot)
        policy = jsonable(validation_policy)
        record = ValidationReportRecord(
            id=str(report_id),
            protocol_id=str(protocol_id),
            status=enum_value(status),
            protocol_hash=protocol_hash,
            capability_snapshot=capability,
            capability_snapshot_hash=capability_snapshot_hash or snapshot_hash(capability),
            validation_policy_snapshot=policy,
            validation_policy_hash=validation_policy_hash or snapshot_hash(policy),
            validator_version=validator_version,
            validated_at=_now(validated_at),
        )
        self.session.add(record)
        self.session.flush()
        for ordinal, issue in enumerate(issues):
            data = _as_mapping(issue)
            self.session.add(
                ValidationIssueRecord(
                    id=str(data.get("issue_id") or data.get("id") or uuid_string()),
                    validation_report_id=record.id,
                    ordinal=ordinal,
                    code=str(data["code"]),
                    severity=enum_value(data["severity"]),
                    path=str(data.get("path", "")),
                    message=str(data["message"]),
                    rule_name=str(data["rule_name"]),
                    rule_version=str(data["rule_version"]),
                    details=jsonable(data.get("details", {})),
                )
            )
        return record

    def begin_execution(
        self,
        *,
        run_id: str,
        experiment_id: str,
        protocol_id: str,
        validation_report_id: str,
        backend_name: str,
        backend_version: str | None = None,
        device_snapshot: Any = None,
        attempt: int = 1,
        started_at: datetime | None = None,
    ) -> ExecutionRunRecord:
        record = ExecutionRunRecord(
            id=str(run_id),
            experiment_id=str(experiment_id),
            protocol_id=str(protocol_id),
            validation_report_id=str(validation_report_id),
            attempt=attempt,
            status="running",
            backend_name=backend_name,
            backend_version=backend_version,
            device_snapshot=jsonable(device_snapshot or {}),
            started_at=_now(started_at),
        )
        self.session.add(record)
        self.session.flush()
        return record

    def finish_execution(
        self,
        run_id: str,
        *,
        status: str,
        failure_category: str | None = None,
        failure_message: str | None = None,
        safe_state_reached: bool | None = None,
        completed_at: datetime | None = None,
    ) -> ExecutionRunRecord:
        record = _required(self.session, ExecutionRunRecord, str(run_id))
        record.status = enum_value(status)
        record.failure_category = failure_category
        record.failure_message = failure_message
        record.safe_state_reached = safe_state_reached
        record.completed_at = _now(completed_at)
        return record

    def append_action_event(
        self,
        *,
        run_id: str,
        sequence: int,
        step_id: str,
        event_type: str,
        status: str,
        idempotency_key: str,
        retry_safe: bool,
        command: Any,
        event_id: str | None = None,
        response: Any | None = None,
        device_id: str | None = None,
        attempt: int = 1,
        occurred_at: datetime | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
        details: Any = None,
    ) -> ActionEventRecord:
        record = ActionEventRecord(
            id=str(event_id or uuid_string()),
            execution_run_id=str(run_id),
            sequence=sequence,
            step_id=step_id,
            event_type=event_type,
            status=enum_value(status),
            idempotency_key=idempotency_key,
            retry_safe=retry_safe,
            attempt=attempt,
            device_id=device_id,
            command_snapshot=jsonable(command),
            response_snapshot=None if response is None else jsonable(response),
            occurred_at=_now(occurred_at),
            started_at=started_at,
            completed_at=completed_at,
            error_type=error_type,
            error_message=error_message,
            details=jsonable(details or {}),
        )
        self.session.add(record)
        self.session.flush()
        return record

    def add_measurement(
        self,
        *,
        measurement_id: str,
        experiment_id: str,
        run_id: str,
        measurement_type: str,
        sample_role: str,
        value: float,
        unit: str,
        raw_payload: Any,
        provenance: Any,
        measured_at: datetime,
        action_event_id: str | None = None,
        well_id: str | None = None,
        analyte_level: float | None = None,
        replicate_index: int | None = None,
        quality_flags: Iterable[Any] = (),
    ) -> MeasurementRecord:
        record = MeasurementRecord(
            id=str(measurement_id),
            experiment_id=str(experiment_id),
            execution_run_id=str(run_id),
            action_event_id=None if action_event_id is None else str(action_event_id),
            measurement_type=measurement_type,
            sample_role=sample_role,
            well_id=well_id,
            analyte_level=analyte_level,
            replicate_index=replicate_index,
            value=float(value),
            unit=unit,
            quality_flags=jsonable(list(quality_flags)),
            raw_payload=jsonable(raw_payload),
            provenance=jsonable(provenance),
            measured_at=measured_at,
        )
        self.session.add(record)
        self.session.flush()
        return record

    def add_measurements(
        self, measurements: Iterable[Mapping[str, Any]]
    ) -> list[MeasurementRecord]:
        return [self.add_measurement(**dict(measurement)) for measurement in measurements]

    def add_qc_run(
        self,
        *,
        qc_run_id: str,
        experiment_id: str,
        run_id: str,
        status: str,
        eligible_for_optimization: bool,
        algorithm_name: str,
        algorithm_version: str,
        checks: Iterable[Any] = (),
        summary: Any = None,
        version: int = 1,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> QCRunRecord:
        record = QCRunRecord(
            id=str(qc_run_id),
            experiment_id=str(experiment_id),
            execution_run_id=str(run_id),
            version=version,
            status=enum_value(status),
            eligible_for_optimization=eligible_for_optimization,
            algorithm_name=algorithm_name,
            algorithm_version=algorithm_version,
            summary=jsonable(summary or {}),
            started_at=_now(started_at),
            completed_at=completed_at or utc_now(),
        )
        self.session.add(record)
        self.session.flush()
        for check in checks:
            data = _as_mapping(check)
            self.session.add(
                QCCheckRecord(
                    id=str(data.get("check_id") or data.get("id") or uuid_string()),
                    qc_run_id=record.id,
                    name=str(data["name"]),
                    status=enum_value(data["status"]),
                    method=str(data.get("method", data["name"])),
                    threshold=(
                        jsonable(data["threshold"]) if data.get("threshold") is not None else None
                    ),
                    observed=(
                        jsonable(data["observed"]) if data.get("observed") is not None else None
                    ),
                    flagged_measurement_ids=jsonable(data.get("flagged_measurement_ids", [])),
                    recomputed_with_exclusions=bool(data.get("recomputed_with_exclusions", False)),
                    details=jsonable(data.get("details", {})),
                )
            )
        return record

    def add_metric(
        self,
        *,
        experiment_id: str,
        name: str,
        value: float,
        unit: str,
        algorithm_name: str,
        algorithm_version: str,
        metric_id: str | None = None,
        qc_run_id: str | None = None,
        uncertainty: Any | None = None,
        source_measurement_ids: Iterable[str] = (),
        details: Any = None,
        created_at: datetime | None = None,
    ) -> DerivedMetricRecord:
        record = DerivedMetricRecord(
            id=str(metric_id or uuid_string()),
            experiment_id=str(experiment_id),
            qc_run_id=None if qc_run_id is None else str(qc_run_id),
            name=name,
            value=float(value),
            unit=unit,
            uncertainty=None if uncertainty is None else jsonable(uncertainty),
            source_measurement_ids=jsonable(list(source_measurement_ids)),
            algorithm_name=algorithm_name,
            algorithm_version=algorithm_version,
            details=jsonable(details or {}),
            created_at=_now(created_at),
        )
        self.session.add(record)
        self.session.flush()
        return record

    def add_constraint_result(
        self,
        *,
        experiment_id: str,
        metric_name: str,
        operator: str,
        threshold: float,
        actual: float,
        residual: float,
        feasible: bool,
        constraint_id: str | None = None,
        details: Any = None,
        evaluated_at: datetime | None = None,
    ) -> PerformanceConstraintRecord:
        record = PerformanceConstraintRecord(
            id=str(constraint_id or uuid_string()),
            experiment_id=str(experiment_id),
            metric_name=metric_name,
            operator=operator,
            threshold=float(threshold),
            actual=float(actual),
            residual=float(residual),
            feasible=feasible,
            details=jsonable(details or {}),
            evaluated_at=_now(evaluated_at),
        )
        self.session.add(record)
        return record

    def add_experiment_result(
        self,
        *,
        result_id: str,
        experiment_id: str,
        run_id: str,
        result: Any,
        optimizer_eligible: bool,
        qc_run_id: str | None = None,
        schema_version: str = "1",
        completed_at: datetime | None = None,
    ) -> ExperimentResultRecord:
        record = ExperimentResultRecord(
            id=str(result_id),
            experiment_id=str(experiment_id),
            execution_run_id=str(run_id),
            qc_run_id=None if qc_run_id is None else str(qc_run_id),
            schema_version=schema_version,
            result_snapshot=jsonable(result),
            optimizer_eligible=optimizer_eligible,
            completed_at=_now(completed_at),
        )
        self.session.add(record)
        return record

    def experiment_evidence(self, experiment_id: str) -> dict[str, Any]:
        identifier = str(experiment_id)
        experiment = _required(self.session, ExperimentRecord, identifier)
        proposal = self.session.scalar(
            select(ProposalRecord).where(ProposalRecord.experiment_id == identifier)
        )
        protocols = list(
            self.session.scalars(
                select(ProtocolRecord)
                .where(ProtocolRecord.experiment_id == identifier)
                .order_by(ProtocolRecord.version)
            )
        )
        protocol_ids = [item.id for item in protocols]
        validations = (
            list(
                self.session.scalars(
                    select(ValidationReportRecord).where(
                        ValidationReportRecord.protocol_id.in_(protocol_ids)
                    )
                )
            )
            if protocol_ids
            else []
        )
        return {
            "experiment": experiment,
            "proposal": proposal,
            "protocols": protocols,
            "validations": validations,
            "runs": list(
                self.session.scalars(
                    select(ExecutionRunRecord).where(ExecutionRunRecord.experiment_id == identifier)
                )
            ),
            "measurements": list(
                self.session.scalars(
                    select(MeasurementRecord).where(MeasurementRecord.experiment_id == identifier)
                )
            ),
            "expected_measurement_plans": list(
                self.session.scalars(
                    select(ExpectedMeasurementPlanRecord).where(
                        ExpectedMeasurementPlanRecord.experiment_id == identifier
                    )
                )
            ),
            "measurement_ingestion_batches": list(
                self.session.scalars(
                    select(MeasurementIngestionBatchRecord).where(
                        MeasurementIngestionBatchRecord.experiment_id == identifier
                    )
                )
            ),
            "external_measurement_revisions": list(
                self.session.scalars(
                    select(ExternalMeasurementRevisionRecord).where(
                        ExternalMeasurementRevisionRecord.experiment_id == identifier
                    )
                )
            ),
            "qc_runs": list(
                self.session.scalars(
                    select(QCRunRecord).where(QCRunRecord.experiment_id == identifier)
                )
            ),
            "metrics": list(
                self.session.scalars(
                    select(DerivedMetricRecord).where(
                        DerivedMetricRecord.experiment_id == identifier
                    )
                )
            ),
            "constraints": list(
                self.session.scalars(
                    select(PerformanceConstraintRecord).where(
                        PerformanceConstraintRecord.experiment_id == identifier
                    )
                )
            ),
            "result": self.session.scalar(
                select(ExperimentResultRecord).where(
                    ExperimentResultRecord.experiment_id == identifier
                )
            ),
        }


class OptimizerRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add_run(
        self,
        *,
        optimizer_run_id: str,
        campaign_id: str,
        strategy_name: str,
        strategy_version: str,
        seed: int,
        config: Any,
        status: str = "running",
        status_reason: str | None = None,
        external_storage_uri: str | None = None,
        created_at: datetime | None = None,
    ) -> OptimizerRunRecord:
        record = OptimizerRunRecord(
            id=str(optimizer_run_id),
            campaign_id=str(campaign_id),
            strategy_name=strategy_name,
            strategy_version=strategy_version,
            seed=seed,
            status=enum_value(status),
            status_reason=status_reason,
            config_snapshot=jsonable(config),
            external_storage_uri=external_storage_uri,
            created_at=_now(created_at),
        )
        self.session.add(record)
        self.session.flush()
        return record

    def get_run(self, optimizer_run_id: str) -> OptimizerRunRecord:
        return _required(self.session, OptimizerRunRecord, str(optimizer_run_id))

    def finish_run(
        self,
        optimizer_run_id: str,
        *,
        status: str = "completed",
        completed_at: datetime | None = None,
    ) -> OptimizerRunRecord:
        record = self.get_run(optimizer_run_id)
        record.status = enum_value(status)
        record.status_reason = None
        record.completed_at = _now(completed_at)
        return record

    def add_observation(
        self,
        *,
        optimizer_run_id: str,
        experiment_id: str,
        trial_id: str,
        objective_values: Any,
        constraint_residuals: Any,
        qc_eligible: bool,
        feasible: bool,
        observation: Any,
        observation_id: str | None = None,
        observed_at: datetime | None = None,
    ) -> OptimizerObservationRecord:
        """Record the sole optimizer observation for a trial/experiment pair.

        Database uniqueness constraints make crash recovery idempotent: a
        completed experiment cannot be reported to the optimizer twice.
        """

        # Related scientific evidence may have been appended earlier in the
        # same unit of work while SQLAlchemy autoflush is intentionally off.
        self.session.flush()
        run = self.get_run(optimizer_run_id)
        experiment = _required(self.session, ExperimentRecord, str(experiment_id))
        if run.campaign_id != experiment.campaign_id:
            raise ValueError("optimizer run and experiment belong to different campaigns")
        if experiment.state != ExperimentState.ANALYSED.value:
            raise ValueError("optimizer observations require an analysed experiment")
        if experiment.optimizer_trial_id not in {None, str(trial_id)}:
            raise ValueError("optimizer observation trial differs from experiment trial")
        if not qc_eligible:
            raise ValueError("an optimizer observation must be QC eligible")
        result = self.session.scalar(
            select(ExperimentResultRecord).where(
                ExperimentResultRecord.experiment_id == experiment.id,
                ExperimentResultRecord.optimizer_eligible.is_(True),
            )
        )
        if result is None or result.qc_run_id is None:
            raise ValueError("optimizer observation requires an eligible experiment result")
        qc_run = self.session.get(QCRunRecord, result.qc_run_id)
        if (
            qc_run is None
            or qc_run.status.lower() != "passed"
            or not qc_run.eligible_for_optimization
        ):
            raise ValueError("optimizer observation requires a passing eligible QC run")
        record = OptimizerObservationRecord(
            id=str(observation_id or uuid_string()),
            optimizer_run_id=str(optimizer_run_id),
            experiment_id=str(experiment_id),
            trial_id=str(trial_id),
            objective_values=jsonable(objective_values),
            constraint_residuals=jsonable(constraint_residuals),
            qc_eligible=qc_eligible,
            feasible=feasible,
            observation_snapshot=jsonable(observation),
            observed_at=_now(observed_at),
        )
        self.session.add(record)
        return record

    def has_observation(self, optimizer_run_id: str, experiment_id: str) -> bool:
        return (
            self.session.scalar(
                select(OptimizerObservationRecord.id).where(
                    OptimizerObservationRecord.optimizer_run_id == str(optimizer_run_id),
                    OptimizerObservationRecord.experiment_id == str(experiment_id),
                )
            )
            is not None
        )

    def unobserved_eligible_experiment_ids(self, optimizer_run_id: str) -> list[str]:
        run = self.get_run(optimizer_run_id)
        observed = select(OptimizerObservationRecord.experiment_id).where(
            OptimizerObservationRecord.optimizer_run_id == run.id
        )
        return list(
            self.session.scalars(
                select(ExperimentRecord.id)
                .join(QCRunRecord, QCRunRecord.experiment_id == ExperimentRecord.id)
                .where(
                    ExperimentRecord.campaign_id == run.campaign_id,
                    QCRunRecord.eligible_for_optimization.is_(True),
                    ExperimentRecord.id.not_in(observed),
                )
                .order_by(ExperimentRecord.ordinal)
            )
        )

    def add_checkpoint(
        self,
        *,
        optimizer_run_id: str,
        ordinal: int,
        state: Any,
        checkpoint_id: str | None = None,
        created_at: datetime | None = None,
    ) -> OptimizerCheckpointRecord:
        state_snapshot = jsonable(state)
        record = OptimizerCheckpointRecord(
            id=str(checkpoint_id or uuid_string()),
            optimizer_run_id=str(optimizer_run_id),
            ordinal=ordinal,
            state_snapshot=state_snapshot,
            state_sha256=snapshot_hash(state_snapshot),
            created_at=_now(created_at),
        )
        self.session.add(record)
        return record

    def latest_checkpoint(self, optimizer_run_id: str) -> OptimizerCheckpointRecord | None:
        return self.session.scalar(
            select(OptimizerCheckpointRecord)
            .where(OptimizerCheckpointRecord.optimizer_run_id == str(optimizer_run_id))
            .order_by(OptimizerCheckpointRecord.ordinal.desc())
            .limit(1)
        )


class ProvenanceRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add_artifact(
        self,
        *,
        campaign_id: str,
        kind: str,
        uri: str,
        sha256: str,
        media_type: str,
        byte_count: int,
        experiment_id: str | None = None,
        metadata: Any = None,
        artifact_id: str | None = None,
        created_at: datetime | None = None,
    ) -> ArtifactRecord:
        record = ArtifactRecord(
            id=str(artifact_id or uuid_string()),
            campaign_id=str(campaign_id),
            experiment_id=None if experiment_id is None else str(experiment_id),
            kind=kind,
            uri=uri,
            sha256=sha256,
            media_type=media_type,
            byte_count=byte_count,
            metadata_json=jsonable(metadata or {}),
            created_at=_now(created_at),
        )
        self.session.add(record)
        return record

    def append_audit(
        self,
        *,
        campaign_id: str,
        entity_type: str,
        entity_id: str,
        event_type: str,
        experiment_id: str | None = None,
        previous_state: str | None = None,
        new_state: str | None = None,
        actor: str = "system",
        reason: str | None = None,
        details: Any = None,
        occurred_at: datetime | None = None,
    ) -> AuditEventRecord:
        record = AuditEventRecord(
            campaign_id=str(campaign_id),
            experiment_id=None if experiment_id is None else str(experiment_id),
            entity_type=entity_type,
            entity_id=str(entity_id),
            event_type=event_type,
            previous_state=previous_state,
            new_state=new_state,
            actor=actor,
            reason=reason,
            details=jsonable(details or {}),
            occurred_at=_now(occurred_at),
        )
        self.session.add(record)
        return record

    def audit_trail(self, campaign_id: str) -> list[AuditEventRecord]:
        return list(
            self.session.scalars(
                select(AuditEventRecord)
                .where(AuditEventRecord.campaign_id == str(campaign_id))
                .order_by(AuditEventRecord.sequence)
            )
        )


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, Any], value)
        return {str(key): item for key, item in mapping.items()}
    dumped = getattr(value, "model_dump", None)
    if callable(dumped):
        result = dumped(mode="json")
        if isinstance(result, Mapping):
            mapping = cast(Mapping[object, Any], result)
            return {str(key): item for key, item in mapping.items()}
    raise TypeError(f"Expected a mapping or Pydantic model, got {type(value).__name__}")
