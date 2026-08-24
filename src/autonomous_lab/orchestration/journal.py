"""Execution journal interfaces and SQLite-backed implementation."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

from autonomous_lab.domain.measurements import Measurement
from autonomous_lab.domain.protocols import ExecutableProtocol, ProtocolStep
from autonomous_lab.hardware.capabilities import CapabilitySnapshot
from autonomous_lab.hardware.commands import DeviceResponse, ExecutionContext
from autonomous_lab.hardware.errors import DeviceExecutionError
from autonomous_lab.storage.unit_of_work import Storage


class ExecutionJournal(Protocol):
    def begin_execution(
        self,
        executable: ExecutableProtocol,
        *,
        run_id: UUID,
        capabilities: CapabilitySnapshot,
    ) -> None: ...

    def record_step_started(
        self,
        step: ProtocolStep,
        context: ExecutionContext,
        *,
        device_id: str,
    ) -> UUID: ...

    def record_step_succeeded(
        self,
        step: ProtocolStep,
        context: ExecutionContext,
        response: DeviceResponse,
        *,
        sequence: int,
    ) -> UUID: ...

    def record_step_failed(
        self,
        step: ProtocolStep,
        context: ExecutionContext,
        error: DeviceExecutionError,
        *,
        device_id: str,
        sequence: int,
        completed_at: datetime,
    ) -> UUID: ...

    def finish_execution(
        self,
        run_id: UUID,
        *,
        status: str,
        failure_category: str | None,
        failure_message: str | None,
        safe_state_reached: bool | None,
    ) -> None: ...


class InMemoryExecutionJournal:
    """Observable journal used by unit tests and storage-free simulations."""

    def __init__(self) -> None:
        self.begun: list[UUID] = []
        self.events: list[dict[str, object]] = []
        self.measurements: list[Measurement] = []
        self.finished: list[dict[str, object]] = []

    def begin_execution(
        self,
        executable: ExecutableProtocol,
        *,
        run_id: UUID,
        capabilities: CapabilitySnapshot,
    ) -> None:
        self.begun.append(run_id)
        self.events.append(
            {
                "kind": "execution_started",
                "run_id": run_id,
                "experiment_id": executable.protocol.experiment_id,
                "capability_snapshot_hash": capabilities.sha256,
            }
        )

    def record_step_started(
        self,
        step: ProtocolStep,
        context: ExecutionContext,
        *,
        device_id: str,
    ) -> UUID:
        event_id = uuid4()
        self.events.append(
            {
                "kind": "step_started",
                "event_id": event_id,
                "step_id": step.step_id,
                "attempt": context.attempt,
                "device_id": device_id,
                "sequence": context.sequence,
            }
        )
        return event_id

    def record_step_succeeded(
        self,
        step: ProtocolStep,
        context: ExecutionContext,
        response: DeviceResponse,
        *,
        sequence: int,
    ) -> UUID:
        event_id = uuid4()
        self.events.append(
            {
                "kind": "step_succeeded",
                "event_id": event_id,
                "step_id": step.step_id,
                "attempt": context.attempt,
                "device_id": response.device_id,
                "sequence": sequence,
            }
        )
        self.measurements.extend(response.measurements)
        return event_id

    def record_step_failed(
        self,
        step: ProtocolStep,
        context: ExecutionContext,
        error: DeviceExecutionError,
        *,
        device_id: str,
        sequence: int,
        completed_at: datetime,
    ) -> UUID:
        event_id = uuid4()
        self.events.append(
            {
                "kind": "step_failed",
                "event_id": event_id,
                "step_id": step.step_id,
                "attempt": context.attempt,
                "device_id": device_id,
                "sequence": sequence,
                "category": error.category,
                "ambiguous": error.ambiguous,
                "completed_at": completed_at,
            }
        )
        return event_id

    def finish_execution(
        self,
        run_id: UUID,
        *,
        status: str,
        failure_category: str | None,
        failure_message: str | None,
        safe_state_reached: bool | None,
    ) -> None:
        self.finished.append(
            {
                "run_id": run_id,
                "status": status,
                "failure_category": failure_category,
                "failure_message": failure_message,
                "safe_state_reached": safe_state_reached,
            }
        )


class StorageExecutionJournal:
    """Commit each action boundary before allowing execution to continue."""

    def __init__(
        self,
        storage: Storage,
        *,
        backend_name: str,
        backend_version: str,
        execution_attempt: int = 1,
    ) -> None:
        self._storage = storage
        self._backend_name = backend_name
        self._backend_version = backend_version
        self._execution_attempt = execution_attempt

    def begin_execution(
        self,
        executable: ExecutableProtocol,
        *,
        run_id: UUID,
        capabilities: CapabilitySnapshot,
    ) -> None:
        with self._storage.unit_of_work() as uow:
            uow.records.begin_execution(
                run_id=str(run_id),
                experiment_id=str(executable.protocol.experiment_id),
                protocol_id=str(executable.protocol.protocol_id),
                validation_report_id=str(executable.validation_report_id),
                backend_name=self._backend_name,
                backend_version=self._backend_version,
                device_snapshot=capabilities,
                attempt=self._execution_attempt,
            )
            uow.commit()

    def record_step_started(
        self,
        step: ProtocolStep,
        context: ExecutionContext,
        *,
        device_id: str,
    ) -> UUID:
        event_id = uuid4()
        with self._storage.unit_of_work() as uow:
            uow.records.append_action_event(
                event_id=str(event_id),
                run_id=str(context.run_id),
                sequence=context.sequence,
                step_id=step.step_id,
                event_type="step.started",
                status="running",
                idempotency_key=context.idempotency_key,
                retry_safe=step.retry_safe,
                attempt=context.attempt,
                device_id=device_id,
                command=step.command,
                occurred_at=context.dispatched_at,
                started_at=context.dispatched_at,
            )
            uow.commit()
        return event_id

    def record_step_succeeded(
        self,
        step: ProtocolStep,
        context: ExecutionContext,
        response: DeviceResponse,
        *,
        sequence: int,
    ) -> UUID:
        event_id = uuid4()
        with self._storage.unit_of_work() as uow:
            uow.records.append_action_event(
                event_id=str(event_id),
                run_id=str(context.run_id),
                sequence=sequence,
                step_id=step.step_id,
                event_type="step.completed",
                status="succeeded",
                idempotency_key=context.idempotency_key,
                retry_safe=step.retry_safe,
                attempt=context.attempt,
                device_id=response.device_id,
                command=step.command,
                response=response,
                occurred_at=response.completed_at,
                started_at=context.dispatched_at,
                completed_at=response.completed_at,
            )
            for measurement in response.measurements:
                uow.records.add_measurement(
                    measurement_id=str(measurement.measurement_id),
                    experiment_id=str(measurement.experiment_id),
                    run_id=str(measurement.run_id),
                    action_event_id=str(event_id),
                    measurement_type=measurement.measurement_name,
                    sample_role=measurement.role.value,
                    well_id=measurement.vessel_id,
                    analyte_level=(
                        None
                        if measurement.analyte_level is None
                        else float(measurement.analyte_level)
                    ),
                    replicate_index=measurement.replicate_index,
                    value=float(measurement.raw_value),
                    unit=measurement.unit,
                    raw_payload=measurement,
                    provenance={
                        "experiment_id": str(measurement.experiment_id),
                        "run_id": str(measurement.run_id),
                        "step_id": measurement.step_id,
                        "device_id": measurement.device_id,
                        **measurement.metadata,
                    },
                    measured_at=measurement.measured_at,
                )
            uow.commit()
        return event_id

    def record_step_failed(
        self,
        step: ProtocolStep,
        context: ExecutionContext,
        error: DeviceExecutionError,
        *,
        device_id: str,
        sequence: int,
        completed_at: datetime,
    ) -> UUID:
        event_id = uuid4()
        with self._storage.unit_of_work() as uow:
            uow.records.append_action_event(
                event_id=str(event_id),
                run_id=str(context.run_id),
                sequence=sequence,
                step_id=step.step_id,
                event_type="step.failed",
                status="failed",
                idempotency_key=context.idempotency_key,
                retry_safe=step.retry_safe,
                attempt=context.attempt,
                device_id=device_id,
                command=step.command,
                occurred_at=completed_at,
                started_at=context.dispatched_at,
                completed_at=completed_at,
                error_type=error.category,
                error_message=str(error),
                details={"ambiguous": error.ambiguous, "device_retry_safe": error.retry_safe},
            )
            uow.commit()
        return event_id

    def finish_execution(
        self,
        run_id: UUID,
        *,
        status: str,
        failure_category: str | None,
        failure_message: str | None,
        safe_state_reached: bool | None,
    ) -> None:
        with self._storage.unit_of_work() as uow:
            uow.records.finish_execution(
                str(run_id),
                status=status,
                failure_category=failure_category,
                failure_message=failure_message,
                safe_state_reached=safe_state_reached,
            )
            uow.commit()
