"""Pure application service for persistent, asynchronous human execution workflows."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID, uuid5

from pydantic import field_validator, model_validator

from autonomous_lab.domain.base import DomainModel
from autonomous_lab.domain.enums import ExecutionMode, ExecutorType
from autonomous_lab.domain.identifiers import require_utc, utc_now
from autonomous_lab.domain.protocols import (
    AspirateCommand,
    DispenseCommand,
    ExecutableProtocol,
    MixCommand,
    ProtocolCommand,
    TransferCommand,
)
from autonomous_lab.domain.quantities import QuantityValue
from autonomous_lab.human.models import (
    AcknowledgementDecision,
    ExecutionConfidence,
    ExecutionStatus,
    HumanExecutionRecord,
    ProtocolDeviation,
    StepAcknowledgement,
    StepExecutionEvidence,
)
from autonomous_lab.human.worklists import OperatorWorklist, WorklistGenerator

_HUMAN_SESSION_NAMESPACE = UUID("a995aa2f-410b-43f8-ab7e-9287aeafba7d")


class HumanExecutionSession(DomainModel):
    """Serializable state that can be saved while an operator is away."""

    schema_version: Literal["1"] = "1"
    session_id: UUID
    worklist: OperatorWorklist
    operator_id: str
    status: Literal[
        ExecutionStatus.IN_PROGRESS,
        ExecutionStatus.FAILED,
        ExecutionStatus.ABORTED,
    ]
    started_at: datetime
    acknowledgements: tuple[StepAcknowledgement, ...] = ()

    @field_validator("operator_id")
    @classmethod
    def _nonempty_operator(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("operator_id must not be empty")
        return value.strip()

    @field_validator("started_at")
    @classmethod
    def _utc_started_at(cls, value: datetime) -> datetime:
        return require_utc(value)

    @model_validator(mode="after")
    def _acknowledgements_belong_to_session(self) -> HumanExecutionSession:
        known_groups = {group.group_id: group.step_ids for group in self.worklist.procedure}
        acknowledged_groups: set[str] = set()
        for item in self.acknowledgements:
            if item.operator_id != self.operator_id:
                raise ValueError("acknowledgement operator does not match session operator")
            if item.group_id not in known_groups or item.step_ids != known_groups[item.group_id]:
                raise ValueError("acknowledgement does not match an exact worklist group")
            if item.group_id in acknowledged_groups:
                raise ValueError("a worklist group cannot be acknowledged twice")
            acknowledged_groups.add(item.group_id)
        return self


class HumanExecutionBackend:
    """Drive operator acknowledgements without assuming a continuously running process.

    The service performs no I/O and owns no mutable state.  Callers persist the
    returned immutable session after every operation, which makes pauses and
    later CLI resumes explicit and crash-safe at the application boundary.
    """

    backend_name = "human_execution"
    backend_version = "1"

    def __init__(self, worklist_generator: WorklistGenerator | None = None) -> None:
        self._worklist_generator = worklist_generator or WorklistGenerator()

    def prepare(
        self,
        executable: ExecutableProtocol,
        *,
        campaign_id: UUID,
        campaign_name: str,
        execution_mode: Literal[ExecutionMode.HUMAN_MANUAL, ExecutionMode.HUMAN_SENSOR],
        operator_id: str | None = None,
        generated_at: datetime | None = None,
    ) -> OperatorWorklist:
        return self._worklist_generator.generate(
            executable,
            campaign_id=campaign_id,
            campaign_name=campaign_name,
            execution_mode=execution_mode,
            operator_id=operator_id,
            generated_at=generated_at,
        )

    def start(
        self,
        worklist: OperatorWorklist,
        *,
        operator_id: str,
        started_at: datetime | None = None,
    ) -> HumanExecutionSession:
        start = started_at or utc_now()
        return HumanExecutionSession(
            session_id=uuid5(
                _HUMAN_SESSION_NAMESPACE,
                f"{worklist.worklist_id}:{operator_id}:{start.isoformat()}",
            ),
            worklist=worklist,
            operator_id=operator_id,
            status=ExecutionStatus.IN_PROGRESS,
            started_at=start,
        )

    def acknowledge(
        self,
        session: HumanExecutionSession,
        *,
        group_id: str,
        decision: AcknowledgementDecision,
        acknowledged_at: datetime | None = None,
        actual_quantities: dict[str, QuantityValue] | None = None,
        deviations: tuple[ProtocolDeviation, ...] = (),
        note: str | None = None,
        evidence_artifact_ids: tuple[str, ...] = (),
    ) -> HumanExecutionSession:
        if session.status is not ExecutionStatus.IN_PROGRESS:
            raise ValueError("cannot acknowledge a terminal human execution session")
        try:
            group = next(item for item in session.worklist.procedure if item.group_id == group_id)
        except StopIteration as exc:
            raise ValueError(f"unknown worklist group: {group_id!r}") from exc
        if any(item.group_id == group_id for item in session.acknowledgements):
            raise ValueError(f"worklist group {group_id!r} was already acknowledged")
        timestamp = acknowledged_at or utc_now()
        acknowledgement = StepAcknowledgement(
            acknowledgement_id=uuid5(
                _HUMAN_SESSION_NAMESPACE,
                f"{session.session_id}:{group_id}:{len(session.acknowledgements)}",
            ),
            group_id=group_id,
            step_ids=group.step_ids,
            operator_id=session.operator_id,
            decision=decision,
            acknowledged_at=timestamp,
            actual_quantities=actual_quantities or {},
            deviations=deviations,
            note=note,
            evidence_artifact_ids=evidence_artifact_ids,
        )
        new_status: ExecutionStatus = ExecutionStatus.IN_PROGRESS
        if decision is AcknowledgementDecision.ABORT:
            new_status = ExecutionStatus.ABORTED
        elif decision in {
            AcknowledgementDecision.SKIP,
            AcknowledgementDecision.MARK_FAILED,
        }:
            new_status = ExecutionStatus.FAILED
        return session.model_copy(
            update={
                "status": new_status,
                "acknowledgements": (*session.acknowledgements, acknowledgement),
            }
        )

    def complete(
        self,
        session: HumanExecutionSession,
        *,
        completed_at: datetime | None = None,
    ) -> HumanExecutionRecord:
        if session.status is not ExecutionStatus.IN_PROGRESS:
            raise ValueError("only an in-progress human execution session can complete")
        expected = {group.group_id for group in session.worklist.procedure}
        received = {item.group_id for item in session.acknowledgements}
        if missing := sorted(expected - received):
            raise ValueError(f"cannot complete; unacknowledged worklist groups: {missing}")
        disallowed = [
            item.group_id
            for item in session.acknowledgements
            if item.decision
            not in {
                AcknowledgementDecision.CONFIRM,
                AcknowledgementDecision.RECORD_DEVIATION,
            }
        ]
        if disallowed:
            raise ValueError(f"cannot complete; skipped or failed worklist groups: {disallowed}")
        return self._record(session, ExecutionStatus.COMPLETED, completed_at or utc_now())

    def terminal_record(
        self,
        session: HumanExecutionSession,
        *,
        completed_at: datetime | None = None,
    ) -> HumanExecutionRecord:
        if session.status not in {ExecutionStatus.ABORTED, ExecutionStatus.FAILED}:
            raise ValueError("terminal_record requires an aborted or failed session")
        return self._record(session, session.status, completed_at or utc_now())

    def _record(
        self,
        session: HumanExecutionSession,
        status: ExecutionStatus,
        completed_at: datetime,
    ) -> HumanExecutionRecord:
        commands = {step.step_id: step.command for step in session.worklist.requested_steps}
        evidence: list[StepExecutionEvidence] = []
        deviations: list[ProtocolDeviation] = []
        for acknowledgement in session.acknowledgements:
            deviations.extend(acknowledgement.deviations)
            step_status = _evidence_status(acknowledgement.decision)
            for step_id in acknowledgement.step_ids:
                evidence.append(
                    StepExecutionEvidence(
                        evidence_id=uuid5(
                            _HUMAN_SESSION_NAMESPACE,
                            f"{session.session_id}:{acknowledgement.acknowledgement_id}:{step_id}",
                        ),
                        experiment_id=session.worklist.experiment_id,
                        protocol_id=session.worklist.protocol_id,
                        step_id=step_id,
                        group_id=acknowledgement.group_id,
                        executor_type=ExecutorType.HUMAN_OPERATOR,
                        executor_id=session.operator_id,
                        execution_mode=session.worklist.execution_mode,
                        requested_action=commands[step_id],
                        observed_action=f"operator decision: {acknowledgement.decision.value}",
                        started_at=session.started_at,
                        completed_at=acknowledgement.acknowledged_at,
                        status=step_status,
                        confidence=(
                            ExecutionConfidence.UNVERIFIED
                            if step_status is not ExecutionStatus.COMPLETED
                            else ExecutionConfidence.CONFIRMED
                        ),
                        actual_quantities=acknowledgement.actual_quantities,
                        deviations=_deviations_for_step(
                            acknowledgement.deviations, commands[step_id]
                        ),
                        note=acknowledgement.note,
                        evidence_artifact_ids=acknowledgement.evidence_artifact_ids,
                        acknowledgement_id=acknowledgement.acknowledgement_id,
                    )
                )
        return HumanExecutionRecord(
            record_id=uuid5(
                _HUMAN_SESSION_NAMESPACE, f"record:{session.session_id}:{status.value}"
            ),
            experiment_id=session.worklist.experiment_id,
            protocol_id=session.worklist.protocol_id,
            worklist_id=session.worklist.worklist_id,
            operator_id=session.operator_id,
            execution_mode=session.worklist.execution_mode,
            status=status,
            started_at=session.started_at,
            completed_at=completed_at,
            acknowledgements=session.acknowledgements,
            step_evidence=tuple(evidence),
            deviations=tuple(deviations),
        )


def _evidence_status(decision: AcknowledgementDecision) -> ExecutionStatus:
    return {
        AcknowledgementDecision.CONFIRM: ExecutionStatus.COMPLETED,
        AcknowledgementDecision.RECORD_DEVIATION: ExecutionStatus.COMPLETED,
        AcknowledgementDecision.SKIP: ExecutionStatus.SKIPPED,
        AcknowledgementDecision.ABORT: ExecutionStatus.ABORTED,
        AcknowledgementDecision.MARK_FAILED: ExecutionStatus.FAILED,
    }[decision]


def _deviations_for_step(
    deviations: tuple[ProtocolDeviation, ...],
    command: ProtocolCommand,
) -> tuple[ProtocolDeviation, ...]:
    vessel_ids = _command_vessel_ids(command)
    return tuple(
        deviation
        for deviation in deviations
        if deviation.vessel_id is None or deviation.vessel_id in vessel_ids
    )


def _command_vessel_ids(command: ProtocolCommand) -> tuple[str, ...]:
    if isinstance(command, DispenseCommand):
        return (command.destination_vessel_id,)
    if isinstance(command, AspirateCommand):
        return (command.source_vessel_id,)
    if isinstance(command, TransferCommand):
        return (command.source_vessel_id, command.destination_vessel_id)
    if isinstance(command, MixCommand):
        return (command.vessel_id,)
    return command.vessel_ids
