"""Sequential, deterministic protocol orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from autonomous_lab.domain.identifiers import utc_now
from autonomous_lab.domain.measurements import Measurement
from autonomous_lab.domain.protocols import (
    ExecutableProtocol,
    MeasureAbsorbanceCommand,
    ProtocolStep,
)
from autonomous_lab.hardware.commands import (
    AcquisitionRecord,
    DeviceResponse,
    ExecutionContext,
    ExecutionEvidenceFragment,
)
from autonomous_lab.hardware.errors import (
    AmbiguousDeviceStateError,
    DeviceExecutionError,
    DeviceTimeoutError,
)
from autonomous_lab.hardware.evidence import (
    DefaultEvidenceFinalizer,
    EvidenceFinalizationError,
    EvidenceFinalizer,
)
from autonomous_lab.hardware.interfaces import Device
from autonomous_lab.hardware.registry import DeviceRegistry
from autonomous_lab.orchestration.events import ActionAttempt, ExecutionRecord
from autonomous_lab.orchestration.journal import ExecutionJournal, InMemoryExecutionJournal
from autonomous_lab.orchestration.receipt import ReceiptVerifier
from autonomous_lab.orchestration.retry import RetryPolicy


class LabOrchestrator:
    """Execute only receipt-authorized protocols, one operation at a time."""

    def __init__(
        self,
        registry: DeviceRegistry,
        receipt_verifier: ReceiptVerifier,
        *,
        journal: ExecutionJournal | None = None,
        retry_policy: RetryPolicy | None = None,
        clock: Callable[[], datetime] = utc_now,
        safe_state_timeout_seconds: float = 5.0,
        evidence_finalizer: EvidenceFinalizer | None = None,
    ) -> None:
        if safe_state_timeout_seconds <= 0:
            raise ValueError("safe-state timeout must be positive")
        self._registry = registry
        self._receipt_verifier = receipt_verifier
        self._journal = journal or InMemoryExecutionJournal()
        self._retry_policy = retry_policy or RetryPolicy()
        self._clock = clock
        self._safe_state_timeout_seconds = safe_state_timeout_seconds
        self._evidence_finalizer = evidence_finalizer or DefaultEvidenceFinalizer()

    async def execute(
        self,
        protocol: ExecutableProtocol,
        *,
        run_id: UUID,
    ) -> ExecutionRecord:
        if protocol.__class__ is not ExecutableProtocol:
            raise TypeError("LabOrchestrator accepts only ExecutableProtocol")

        # Receipt checks and device resolution happen before journal creation and,
        # critically, before the first hardware dispatch.
        self._receipt_verifier.verify(protocol, self._registry.snapshot)
        resolved = tuple((step, self._registry.resolve(step)) for step in protocol.protocol.steps)

        started_at = self._clock()
        self._journal.begin_execution(
            protocol,
            run_id=run_id,
            capabilities=self._registry.snapshot,
        )
        action_attempts: list[ActionAttempt] = []
        measurements: list[Measurement] = []
        evidence_fragments: list[ExecutionEvidenceFragment] = []
        event_sequence = 0

        for step, device in resolved:
            attempt = 0
            while True:
                attempt += 1
                idempotency_key = f"{run_id}:{step.step_id}"
                dispatched_at = self._clock()
                context = ExecutionContext(
                    experiment_id=protocol.protocol.experiment_id,
                    run_id=run_id,
                    step_id=step.step_id,
                    sequence=event_sequence,
                    attempt=attempt,
                    idempotency_key=idempotency_key,
                    dispatched_at=dispatched_at,
                )
                self._journal.record_step_started(step, context, device_id=device.device_id)
                event_sequence += 1
                try:
                    response = await self._dispatch_with_timeout(step, device, context)
                    try:
                        self._check_response(response, context, device.device_id, step)
                    except DeviceExecutionError as error:
                        if error.category.startswith(
                            ("evidence_", "acquisition_", "duplicate_evidence_")
                        ):
                            raise DeviceExecutionError(
                                "invalid_execution_evidence",
                                f"{error.category}: {error}",
                                retry_safe=False,
                            ) from error
                        if not step.retry_safe:
                            raise AmbiguousDeviceStateError(
                                error.category,
                                "non-retry-safe command returned an invalid response; "
                                "its physical effect is unknown",
                            ) from error
                        raise
                except asyncio.CancelledError:
                    cancelled_at = self._clock()
                    cancellation_error: DeviceExecutionError
                    if step.retry_safe:
                        cancellation_error = DeviceExecutionError(
                            "execution_cancelled",
                            "execution was cancelled during device dispatch",
                            retry_safe=False,
                        )
                    else:
                        cancellation_error = AmbiguousDeviceStateError(
                            f"{step.command.command_type.value}_cancelled",
                            "execution was cancelled after dispatch and the physical "
                            "effect is unknown",
                        )
                    safe_state_reached = await self._enter_safe_state(cancellation_error.category)
                    self._journal.record_step_failed(
                        step,
                        context,
                        cancellation_error,
                        device_id=device.device_id,
                        sequence=event_sequence,
                        completed_at=cancelled_at,
                    )
                    self._journal.finish_execution(
                        run_id,
                        status="failed",
                        failure_category=cancellation_error.category,
                        failure_message=str(cancellation_error),
                        safe_state_reached=safe_state_reached,
                    )
                    raise
                except DeviceExecutionError as error:
                    failed_at = self._clock()
                    retry = self._retry_policy.should_retry(step, error, attempt=attempt)
                    safe_state_reached: bool | None = None
                    if not retry:
                        safe_state_reached = await self._enter_safe_state(error.category)
                    try:
                        failed_event_id = self._journal.record_step_failed(
                            step,
                            context,
                            error,
                            device_id=device.device_id,
                            sequence=event_sequence,
                            completed_at=failed_at,
                        )
                    except Exception:
                        if safe_state_reached is None:
                            await self._enter_safe_state("execution_journal_failure")
                        raise
                    event_sequence += 1
                    action_attempts.append(
                        ActionAttempt(
                            event_id=failed_event_id,
                            sequence=event_sequence - 1,
                            step_id=step.step_id,
                            idempotency_key=idempotency_key,
                            attempt=attempt,
                            retry_safe=step.retry_safe,
                            device_id=device.device_id,
                            status="failed",
                            started_at=dispatched_at,
                            completed_at=failed_at,
                            error_category=error.category,
                            error_message=str(error),
                            ambiguous=error.ambiguous,
                        )
                    )
                    if retry:
                        continue
                    assert safe_state_reached is not None
                    completed_at = self._clock()
                    self._journal.finish_execution(
                        run_id,
                        status="failed",
                        failure_category=error.category,
                        failure_message=str(error),
                        safe_state_reached=safe_state_reached,
                    )
                    return ExecutionRecord(
                        run_id=run_id,
                        experiment_id=protocol.protocol.experiment_id,
                        protocol_id=protocol.protocol.protocol_id,
                        validation_report_id=protocol.validation_report_id,
                        status="failed",
                        started_at=started_at,
                        completed_at=completed_at,
                        actions=tuple(action_attempts),
                        measurements=tuple(measurements),
                        failure_category=error.category,
                        failure_message=str(error),
                        safe_state_reached=safe_state_reached,
                    )

                try:
                    completed_event_id = self._journal.record_step_succeeded(
                        step,
                        context,
                        response,
                        sequence=event_sequence,
                    )
                except Exception:
                    await self._enter_safe_state("execution_journal_failure")
                    raise
                action_attempts.append(
                    ActionAttempt(
                        event_id=completed_event_id,
                        sequence=event_sequence,
                        step_id=step.step_id,
                        idempotency_key=idempotency_key,
                        attempt=attempt,
                        retry_safe=step.retry_safe,
                        device_id=device.device_id,
                        status="succeeded",
                        started_at=dispatched_at,
                        completed_at=response.completed_at,
                        response=response,
                    )
                )
                event_sequence += 1
                measurements.extend(response.measurements)
                evidence_fragments.extend(response.evidence_fragments)
                break

        completed_at = self._clock()
        try:
            execution_evidence = self._evidence_finalizer.finalize(
                protocol,
                run_id=run_id,
                fragments=tuple(evidence_fragments),
                measurements=tuple(measurements),
                finalized_at=completed_at,
            )
        except EvidenceFinalizationError as error:
            safe_state_reached = await self._enter_safe_state("invalid_execution_evidence")
            self._journal.finish_execution(
                run_id,
                status="failed",
                failure_category="invalid_execution_evidence",
                failure_message=str(error),
                safe_state_reached=safe_state_reached,
            )
            return ExecutionRecord(
                run_id=run_id,
                experiment_id=protocol.protocol.experiment_id,
                protocol_id=protocol.protocol.protocol_id,
                validation_report_id=protocol.validation_report_id,
                status="failed",
                started_at=started_at,
                completed_at=self._clock(),
                actions=tuple(action_attempts),
                measurements=tuple(measurements),
                failure_category="invalid_execution_evidence",
                failure_message=str(error),
                safe_state_reached=safe_state_reached,
            )
        try:
            self._journal.finish_execution(
                run_id,
                status="succeeded",
                failure_category=None,
                failure_message=None,
                safe_state_reached=None,
            )
        except Exception:
            await self._enter_safe_state("execution_journal_failure")
            raise
        return ExecutionRecord(
            run_id=run_id,
            experiment_id=protocol.protocol.experiment_id,
            protocol_id=protocol.protocol.protocol_id,
            validation_report_id=protocol.validation_report_id,
            status="succeeded",
            started_at=started_at,
            completed_at=completed_at,
            actions=tuple(action_attempts),
            measurements=tuple(measurements),
            execution_evidence=execution_evidence,
        )

    async def _dispatch_with_timeout(
        self,
        step: ProtocolStep,
        device: Device,
        context: ExecutionContext,
    ) -> DeviceResponse:
        try:
            return await asyncio.wait_for(
                device.execute(step.command, context),
                timeout=float(Decimal(step.timeout_seconds)),
            )
        except TimeoutError as exc:
            if step.retry_safe and step.command.command_type.value == "measure_absorbance":
                raise DeviceTimeoutError() from exc
            raise AmbiguousDeviceStateError(
                f"{step.command.command_type.value}_timeout",
                "command timed out and its physical effect is unknown",
            ) from exc
        except DeviceExecutionError:
            raise
        except Exception as exc:
            if not step.retry_safe:
                raise AmbiguousDeviceStateError(
                    f"{step.command.command_type.value}_unexpected_error",
                    "command failed after dispatch and its physical effect is unknown",
                ) from exc
            raise DeviceExecutionError(
                "unexpected_device_error",
                str(exc),
                retry_safe=True,
            ) from exc

    async def _enter_safe_state(self, reason: str) -> bool:
        """Complete a bounded safe stop even if the caller is being cancelled."""

        stop = asyncio.create_task(
            self._registry.enter_safe_state(
                reason,
                timeout_seconds=self._safe_state_timeout_seconds,
            )
        )
        try:
            return await asyncio.shield(stop)
        except asyncio.CancelledError:
            await asyncio.shield(stop)
            raise

    @staticmethod
    def _check_response(
        response: DeviceResponse,
        context: ExecutionContext,
        expected_device_id: str,
        step: ProtocolStep,
    ) -> None:
        if response.device_id != expected_device_id:
            raise DeviceExecutionError("response_device_mismatch")
        if response.command_type is not step.command.command_type:
            raise DeviceExecutionError("response_command_mismatch")
        measurement_ids = [item.measurement_id for item in response.measurements]
        if len(measurement_ids) != len(set(measurement_ids)):
            raise DeviceExecutionError("duplicate_measurement_id")
        for measurement in response.measurements:
            if measurement.experiment_id != context.experiment_id:
                raise DeviceExecutionError("measurement_experiment_mismatch")
            if measurement.run_id != context.run_id:
                raise DeviceExecutionError("measurement_run_mismatch")
            if measurement.step_id != context.step_id:
                raise DeviceExecutionError("measurement_step_mismatch")
            if measurement.device_id != expected_device_id:
                raise DeviceExecutionError("measurement_device_mismatch")
            if (
                isinstance(step.command, MeasureAbsorbanceCommand)
                and measurement.vessel_id not in step.command.vessel_ids
            ):
                raise DeviceExecutionError("measurement_vessel_mismatch")
        fragment_ids = [item.fragment_id for item in response.evidence_fragments]
        if len(fragment_ids) != len(set(fragment_ids)):
            raise DeviceExecutionError("duplicate_evidence_fragment_id")
        response_acquisitions: list[AcquisitionRecord] = []
        for fragment in response.evidence_fragments:
            if fragment.experiment_id != context.experiment_id:
                raise DeviceExecutionError("evidence_experiment_mismatch")
            if fragment.run_id != context.run_id:
                raise DeviceExecutionError("evidence_run_mismatch")
            if fragment.source_step_id != context.step_id:
                raise DeviceExecutionError("evidence_step_mismatch")
            if fragment.source_device_id != expected_device_id:
                raise DeviceExecutionError("evidence_device_mismatch")
            response_acquisitions.extend(fragment.acquisitions)
            for acquisition in fragment.acquisitions:
                if acquisition.experiment_id != context.experiment_id:
                    raise DeviceExecutionError("acquisition_experiment_mismatch")
                if acquisition.run_id != context.run_id:
                    raise DeviceExecutionError("acquisition_run_mismatch")
                if acquisition.step_id != context.step_id:
                    raise DeviceExecutionError("acquisition_step_mismatch")
                if acquisition.device_id != expected_device_id:
                    raise DeviceExecutionError("acquisition_device_mismatch")
        acquisition_ids = [item.acquisition_id for item in response_acquisitions]
        if len(acquisition_ids) != len(set(acquisition_ids)):
            raise DeviceExecutionError("duplicate_acquisition_id")
        successful_acquisition_ids = {
            item.acquisition_id for item in response_acquisitions if item.status == "ok"
        }
        if (response_acquisitions or measurement_ids) and successful_acquisition_ids != set(
            measurement_ids
        ):
            raise DeviceExecutionError("acquisition_measurement_mismatch")
        if isinstance(step.command, MeasureAbsorbanceCommand):
            acquisition_vessels = {item.vessel_id for item in response_acquisitions}
            if len(response_acquisitions) != len(
                step.command.vessel_ids
            ) or acquisition_vessels != set(step.command.vessel_ids):
                raise DeviceExecutionError("acquisition_vessel_mismatch")
