"""Persistent application service for human execution and worklist artifacts."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlparse
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select

from autonomous_lab.domain.enums import ExperimentState
from autonomous_lab.domain.identifiers import require_utc
from autonomous_lab.domain.state_machines import transition_experiment
from autonomous_lab.human.backend import HumanExecutionBackend, HumanExecutionSession
from autonomous_lab.human.models import ExecutionStatus, HumanExecutionRecord
from autonomous_lab.human.worklists import (
    OperatorWorklist,
    WorklistExport,
    export_worklist,
)
from autonomous_lab.storage import Storage
from autonomous_lab.storage._serialization import jsonable
from autonomous_lab.storage.artifacts import ArtifactStore
from autonomous_lab.storage.orm import (
    ArtifactRecord,
    AuditEventRecord,
    ExecutionRunRecord,
    ExperimentRecord,
    ValidationReportRecord,
)


class HumanWorkflowError(RuntimeError):
    """Human-loop state or evidence is inconsistent with persisted science."""


class HumanWorkflowService:
    """Persist worklists and truthful operator acknowledgements across processes."""

    def __init__(
        self,
        storage: Storage,
        *,
        worklist_root: str | Path = "data/worklists",
        artifact_root: str | Path = "data/artifacts",
    ) -> None:
        self.storage = storage
        self.worklist_root = Path(worklist_root).expanduser().resolve()
        self.artifact_store = ArtifactStore(artifact_root)

    def persist_worklist(self, worklist: OperatorWorklist) -> WorklistExport:
        """Export human views plus a canonical, content-addressed model snapshot."""

        directory = self.worklist_root / str(worklist.experiment_id)
        exported = export_worklist(worklist, directory)
        model_bytes = json.dumps(
            jsonable(worklist),
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
        model_artifact = self.artifact_store.put_bytes(
            model_bytes,
            media_type="application/json",
            suffix=".json",
        )
        with self.storage.unit_of_work() as uow:
            for item in exported.artifacts:
                path = directory / item.file_name
                content = path.read_bytes()
                if hashlib.sha256(content).hexdigest() != item.sha256:
                    raise HumanWorkflowError("worklist export changed before registration")
                uow.provenance.add_artifact(
                    campaign_id=str(worklist.campaign_id),
                    experiment_id=str(worklist.experiment_id),
                    kind=f"operator_worklist_{item.artifact_type}",
                    uri=path.as_uri(),
                    sha256=item.sha256,
                    media_type=_media_type(item.artifact_type),
                    byte_count=item.byte_count,
                    metadata={
                        "worklist_id": str(worklist.worklist_id),
                        "content_hash": worklist.content_hash,
                    },
                    created_at=worklist.generated_at,
                )
            uow.provenance.add_artifact(
                campaign_id=str(worklist.campaign_id),
                experiment_id=str(worklist.experiment_id),
                kind="operator_worklist_model",
                uri=model_artifact.uri,
                sha256=model_artifact.sha256,
                media_type=model_artifact.media_type,
                byte_count=model_artifact.byte_count,
                metadata={
                    "worklist_id": str(worklist.worklist_id),
                    "content_hash": worklist.content_hash,
                },
                created_at=worklist.generated_at,
            )
            uow.provenance.append_audit(
                campaign_id=str(worklist.campaign_id),
                experiment_id=str(worklist.experiment_id),
                entity_type="worklist",
                entity_id=str(worklist.worklist_id),
                event_type="human.worklist_generated",
                details={
                    "protocol_hash": worklist.protocol_hash,
                    "content_hash": worklist.content_hash,
                    "artifacts": [item.model_dump(mode="json") for item in exported.artifacts],
                },
                occurred_at=worklist.generated_at,
            )
            uow.commit()
        return exported

    def load_worklist(self, experiment_id: UUID) -> OperatorWorklist:
        with self.storage.unit_of_work() as uow:
            assert uow.session is not None
            artifact = uow.session.scalar(
                select(ArtifactRecord)
                .where(
                    ArtifactRecord.experiment_id == str(experiment_id),
                    ArtifactRecord.kind == "operator_worklist_model",
                )
                .order_by(ArtifactRecord.created_at.desc())
                .limit(1)
            )
            uow.commit()
        if artifact is None:
            raise HumanWorkflowError(f"experiment {experiment_id} has no operator worklist")
        path = _file_uri_path(artifact.uri)
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != artifact.sha256:
            raise HumanWorkflowError("persisted worklist artifact failed its SHA-256 check")
        worklist = OperatorWorklist.model_validate_json(content, strict=False)
        if worklist.experiment_id != experiment_id:
            raise HumanWorkflowError("worklist experiment identity does not match request")
        return worklist

    def start_execution(self, experiment_id: UUID, *, operator_id: str) -> UUID:
        if not operator_id.strip():
            raise ValueError("operator_id cannot be blank")
        worklist = self.load_worklist(experiment_id)
        run_id = _human_run_id(experiment_id)
        with self.storage.unit_of_work() as uow:
            experiment = uow.experiments.get(str(experiment_id))
            if experiment.state != ExperimentState.AWAITING_EXECUTION.value:
                raise HumanWorkflowError(
                    f"experiment must await execution, not {experiment.state!r}"
                )
            assert uow.session is not None
            validation = uow.session.scalar(
                select(ValidationReportRecord)
                .where(ValidationReportRecord.protocol_id == str(worklist.protocol_id))
                .order_by(ValidationReportRecord.validated_at.desc())
                .limit(1)
            )
            if validation is None or validation.status != "passed":
                raise HumanWorkflowError("human execution requires a passing validation receipt")
            uow.records.begin_execution(
                run_id=str(run_id),
                experiment_id=str(experiment_id),
                protocol_id=str(worklist.protocol_id),
                validation_report_id=validation.id,
                backend_name="human_operator",
                backend_version="1",
                device_snapshot={
                    "execution_mode": worklist.execution_mode.value,
                    "executor_type": "human_operator",
                    "operator_id": operator_id.strip(),
                    "worklist_id": str(worklist.worklist_id),
                    "worklist_content_hash": worklist.content_hash,
                    "claim": "operator session opened; no step is yet claimed complete",
                },
            )
            _transition(uow, experiment, ExperimentState.RUNNING, actor=operator_id.strip())
            uow.commit()
        return run_id

    def complete_execution(self, record: HumanExecutionRecord) -> UUID:
        if record.status not in {
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
            ExecutionStatus.ABORTED,
        }:
            raise HumanWorkflowError("human record must have a terminal status")
        worklist = self.load_worklist(record.experiment_id)
        _validate_record_against_worklist(record, worklist)
        run_id = _human_run_id(record.experiment_id)
        with self.storage.unit_of_work() as uow:
            experiment = uow.experiments.get(str(record.experiment_id))
            if experiment.state != ExperimentState.RUNNING.value:
                raise HumanWorkflowError(f"experiment must be running, not {experiment.state!r}")
            assert uow.session is not None
            execution_run = uow.session.get(ExecutionRunRecord, str(run_id))
            if execution_run is None:
                raise HumanWorkflowError("human execution run evidence is missing")
            started_by = execution_run.device_snapshot.get("operator_id")
            if started_by != record.operator_id:
                raise HumanWorkflowError(
                    "completion operator does not match the operator who started execution"
                )
            for sequence, evidence in enumerate(record.step_evidence):
                uow.records.append_action_event(
                    run_id=str(run_id),
                    sequence=sequence,
                    step_id=evidence.step_id,
                    event_type="human_step_acknowledged",
                    status=evidence.status.value,
                    idempotency_key=f"human:{record.record_id}:{evidence.step_id}",
                    retry_safe=False,
                    command=evidence.requested_action,
                    response=evidence,
                    device_id=record.operator_id,
                    started_at=evidence.started_at,
                    completed_at=evidence.completed_at,
                    details={
                        "executor_type": evidence.executor_type.value,
                        "execution_mode": evidence.execution_mode.value,
                        "confidence": evidence.confidence.value,
                        "deviations": [
                            item.model_dump(mode="json") for item in evidence.deviations
                        ],
                    },
                )
            succeeded = record.status is ExecutionStatus.COMPLETED
            uow.records.finish_execution(
                str(run_id),
                status=("succeeded" if succeeded else record.status.value),
                failure_category=(None if succeeded else f"operator_{record.status.value}"),
                failure_message=(None if succeeded else record.notes or record.status.value),
                safe_state_reached=(True if succeeded else None),
                completed_at=record.completed_at,
            )
            uow.provenance.append_audit(
                campaign_id=experiment.campaign_id,
                experiment_id=experiment.id,
                entity_type="human_execution",
                entity_id=str(record.record_id),
                event_type=(
                    "human.execution_completed"
                    if succeeded
                    else f"human.execution_{record.status.value}"
                ),
                actor=record.operator_id,
                details=record,
                occurred_at=record.completed_at,
            )
            target = (
                ExperimentState.AWAITING_MEASUREMENTS
                if succeeded
                else (
                    ExperimentState.ABORTED
                    if record.status is ExecutionStatus.ABORTED
                    else ExperimentState.FAILED
                )
            )
            _transition(
                uow,
                experiment,
                target,
                actor=record.operator_id,
                reason=None if succeeded else record.notes or record.status.value,
            )
            uow.commit()
        return run_id

    def execution_session(
        self,
        experiment_id: UUID,
        *,
        operator_id: str,
    ) -> HumanExecutionSession:
        """Restore durable grouped acknowledgements for crash-safe CLI resume."""

        worklist = self.load_worklist(experiment_id)
        with self.storage.unit_of_work() as uow:
            experiment = uow.experiments.get(str(experiment_id))
            if experiment.state != ExperimentState.RUNNING.value:
                raise HumanWorkflowError(f"experiment must be running, not {experiment.state!r}")
            assert uow.session is not None
            run = uow.session.get(ExecutionRunRecord, str(_human_run_id(experiment_id)))
            if run is None or run.status != "running":
                raise HumanWorkflowError("human execution run is not active")
            started_by = run.device_snapshot.get("operator_id")
            if started_by != operator_id.strip():
                raise HumanWorkflowError(
                    "operator does not match the operator who started execution"
                )
            event = uow.session.scalar(
                select(AuditEventRecord)
                .where(
                    AuditEventRecord.experiment_id == str(experiment_id),
                    AuditEventRecord.event_type == "human.execution_progress",
                )
                .order_by(AuditEventRecord.sequence.desc())
                .limit(1)
            )
            started_at = _as_utc(run.started_at)
            uow.commit()
        if event is None:
            return HumanExecutionBackend().start(
                worklist,
                operator_id=operator_id,
                started_at=started_at,
            )
        session = HumanExecutionSession.model_validate(event.details, strict=False)
        if (
            session.worklist.content_hash != worklist.content_hash
            or session.operator_id != operator_id.strip()
            or session.started_at != started_at
        ):
            raise HumanWorkflowError("persisted human execution progress is inconsistent")
        return session

    def persist_execution_session(self, session: HumanExecutionSession) -> None:
        """Append one acknowledgement checkpoint immediately after operator input."""

        if not session.acknowledgements:
            raise HumanWorkflowError("execution progress has no acknowledgement to persist")
        experiment_id = session.worklist.experiment_id
        with self.storage.unit_of_work() as uow:
            experiment = uow.experiments.get(str(experiment_id))
            if experiment.state != ExperimentState.RUNNING.value:
                raise HumanWorkflowError(f"experiment must be running, not {experiment.state!r}")
            assert uow.session is not None
            run = uow.session.get(ExecutionRunRecord, str(_human_run_id(experiment_id)))
            if run is None or run.status != "running":
                raise HumanWorkflowError("human execution run is not active")
            if run.device_snapshot.get("operator_id") != session.operator_id:
                raise HumanWorkflowError("execution progress operator differs from start record")
            previous = uow.session.scalar(
                select(AuditEventRecord)
                .where(
                    AuditEventRecord.experiment_id == str(experiment_id),
                    AuditEventRecord.event_type == "human.execution_progress",
                )
                .order_by(AuditEventRecord.sequence.desc())
                .limit(1)
            )
            previous_session = (
                None
                if previous is None
                else HumanExecutionSession.model_validate(previous.details, strict=False)
            )
            previous_acknowledgements = (
                () if previous_session is None else previous_session.acknowledgements
            )
            if (
                len(session.acknowledgements) != len(previous_acknowledgements) + 1
                or session.acknowledgements[:-1] != previous_acknowledgements
            ):
                raise HumanWorkflowError(
                    "execution progress must append exactly one new acknowledgement"
                )
            latest = session.acknowledgements[-1]
            uow.provenance.append_audit(
                campaign_id=experiment.campaign_id,
                experiment_id=experiment.id,
                entity_type="human_execution_session",
                entity_id=str(session.session_id),
                event_type="human.execution_progress",
                actor=session.operator_id,
                details=session,
                occurred_at=latest.acknowledged_at,
            )
            uow.commit()

    def abort_execution(
        self,
        experiment_id: UUID,
        *,
        operator_id: str,
        reason: str,
    ) -> None:
        if not operator_id.strip() or not reason.strip():
            raise ValueError("operator_id and abort reason are required")
        worklist = self.load_worklist(experiment_id)
        run_id = _human_run_id(experiment_id)
        with self.storage.unit_of_work() as uow:
            experiment = uow.experiments.get(str(experiment_id))
            if experiment.state == ExperimentState.AWAITING_EXECUTION.value:
                assert uow.session is not None
                validation = uow.session.scalar(
                    select(ValidationReportRecord).where(
                        ValidationReportRecord.protocol_id == str(worklist.protocol_id)
                    )
                )
                if validation is None:
                    raise HumanWorkflowError("validated protocol evidence is missing")
                uow.records.begin_execution(
                    run_id=str(run_id),
                    experiment_id=str(experiment_id),
                    protocol_id=str(worklist.protocol_id),
                    validation_report_id=validation.id,
                    backend_name="human_operator",
                    backend_version="1",
                    device_snapshot={"operator_id": operator_id, "aborted_before_start": True},
                )
            elif experiment.state != ExperimentState.RUNNING.value:
                raise HumanWorkflowError(f"cannot abort experiment in {experiment.state!r}")
            else:
                assert uow.session is not None
                execution_run = uow.session.get(ExecutionRunRecord, str(run_id))
                if execution_run is None:
                    raise HumanWorkflowError("human execution run evidence is missing")
                if execution_run.device_snapshot.get("operator_id") != operator_id.strip():
                    raise HumanWorkflowError(
                        "abort operator does not match the operator who started execution"
                    )
            uow.records.finish_execution(
                str(run_id),
                status="aborted",
                failure_category="operator_abort",
                failure_message=reason.strip(),
                safe_state_reached=None,
            )
            uow.provenance.append_audit(
                campaign_id=experiment.campaign_id,
                experiment_id=experiment.id,
                entity_type="human_execution",
                entity_id=str(run_id),
                event_type="human.execution_aborted",
                actor=operator_id.strip(),
                reason=reason.strip(),
            )
            _transition(
                uow,
                experiment,
                ExperimentState.ABORTED,
                actor=operator_id.strip(),
                reason=reason.strip(),
            )
            uow.commit()

    def execution_run(self, experiment_id: UUID) -> ExecutionRunRecord | None:
        with self.storage.unit_of_work() as uow:
            assert uow.session is not None
            run = uow.session.get(ExecutionRunRecord, str(_human_run_id(experiment_id)))
            if run is not None:
                uow.session.expunge(run)
            uow.commit()
        return run


def _transition(
    uow: object,
    experiment: ExperimentRecord,
    target: ExperimentState,
    *,
    actor: str,
    reason: str | None = None,
) -> None:
    transition_experiment(
        UUID(experiment.id),
        ExperimentState(experiment.state),
        target,
        reason=reason,
    )
    uow.experiments.transition(  # type: ignore[attr-defined]
        experiment.id,
        target,
        expected_state=experiment.state,
        actor=actor,
        reason=reason,
    )


def _validate_record_against_worklist(
    record: HumanExecutionRecord,
    worklist: OperatorWorklist,
) -> None:
    if record.experiment_id != worklist.experiment_id:
        raise HumanWorkflowError("human record experiment differs from worklist")
    if record.protocol_id != worklist.protocol_id or record.worklist_id != worklist.worklist_id:
        raise HumanWorkflowError("human record is not bound to the persisted worklist")
    if record.execution_mode != worklist.execution_mode:
        raise HumanWorkflowError("human record execution mode differs from worklist")
    backend = HumanExecutionBackend()
    session = backend.start(
        worklist,
        operator_id=record.operator_id,
        started_at=record.started_at,
    ).model_copy(update={"acknowledgements": record.acknowledgements})
    if record.status is not ExecutionStatus.COMPLETED:
        session = session.model_copy(update={"status": record.status})
    if record.completed_at is None:
        raise HumanWorkflowError("terminal human record has no completion timestamp")
    try:
        canonical = (
            backend.complete(session, completed_at=record.completed_at)
            if record.status is ExecutionStatus.COMPLETED
            else backend.terminal_record(session, completed_at=record.completed_at)
        )
    except ValueError as exc:
        raise HumanWorkflowError(f"invalid human acknowledgement evidence: {exc}") from exc
    if canonical != record:
        raise HumanWorkflowError(
            "human record is not the canonical evidence derived from its acknowledgements"
        )


def _human_run_id(experiment_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"autolab:{experiment_id}:human-execution:1")


def _file_uri_path(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise HumanWorkflowError("worklist model is not a local immutable artifact")
    return Path(unquote(parsed.path)).resolve()


def _media_type(artifact_type: str) -> str:
    return "text/markdown" if artifact_type == "markdown" else "text/csv"


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else require_utc(value)
