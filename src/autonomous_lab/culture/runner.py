"""Run one culture experiment end to end: propose -> validate -> execute -> measure -> QC.

The runner is the only place that joins the culture pieces. It never chooses
conditions (the proposal does), never decides whether a command is safe (the
validator and the device do), and never interprets data (QC and analysis do).

Execution order for one experiment::

    compile -> validate (receipt) -> re-check receipt against the live device
      -> open + self-test sensor -> apply setpoints (acknowledged)
      -> until the hold ends: telemetry every telemetry_interval,
                              one OD reading per plan entry
      -> ALWAYS: request safe state, close sensor
      -> reconcile time series -> culture QC -> analysis (QC passed only)

Every experiment-state change goes through AutoLab's domain state machine, so
the audit trail uses the same legal transitions as assay experiments.

Not in this slice: persistence, campaign wiring, pause/resume across process
restarts, and parallel reactors. See ``docs/culture.md``.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field

from autonomous_lab.culture.analysis import CultureMetrics, analyse_culture
from autonomous_lab.culture.clock import CultureClock
from autonomous_lab.culture.compiler import CultureCompilationError, CultureProtocolCompiler
from autonomous_lab.culture.controller import (
    ControllerError,
    CultureEnvironmentController,
    EnvironmentTelemetry,
    SafeStateAcknowledgement,
    SetpointAcknowledgement,
    SetpointCommand,
)
from autonomous_lab.culture.evidence import CultureRunEvidence, RunEvent
from autonomous_lab.culture.models import (
    CultureDesign,
    CulturePolicy,
    CultureProtocol,
    CultureQCConfig,
    ExecutableCultureProtocol,
    SamplePoint,
    to_timedelta,
)
from autonomous_lab.culture.qc import CultureQCReport, evaluate_culture_qc
from autonomous_lab.culture.timeseries import (
    SampleReading,
    TimeSeriesReconciliation,
    reconcile_time_series,
)
from autonomous_lab.culture.validation import CultureValidationService
from autonomous_lab.domain.base import DomainModel
from autonomous_lab.domain.enums import ExperimentState
from autonomous_lab.domain.experiments import ExperimentProposal
from autonomous_lab.domain.protocols import ValidationReport
from autonomous_lab.domain.state_machines import StateTransition, transition_experiment
from autonomous_lab.hardware.sensors.contracts import (
    MeasurementRequest,
    ReadOnlySensor,
    SelfTestStatus,
    SensorCapabilities,
)
from autonomous_lab.hardware.sensors.errors import SensorError
from autonomous_lab.provenance.hashing import canonical_sha256

Disposition = Literal["rejected", "failed", "qc_failed", "analysed"]


class CultureExperimentRecord(DomainModel):
    """The complete, hashable evidence trail for one culture experiment."""

    schema_version: Literal["1"] = "1"
    experiment_id: UUID
    proposal: ExperimentProposal
    disposition: Disposition
    reason: str | None = None
    protocol: CultureProtocol | None = None
    validation_report: ValidationReport | None = None
    evidence: CultureRunEvidence | None = None
    reconciliation: TimeSeriesReconciliation | None = None
    qc_report: CultureQCReport | None = None
    metrics: CultureMetrics | None = None
    transitions: tuple[StateTransition, ...] = Field(default=())

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


class _Audit:
    """Track experiment state through the shared domain state machine."""

    def __init__(self, experiment_id: UUID) -> None:
        self.experiment_id = experiment_id
        self.state = ExperimentState.PROPOSED
        self.transitions: list[StateTransition] = []

    def to(self, target: ExperimentState, reason: str | None = None) -> None:
        self.transitions.append(
            transition_experiment(self.experiment_id, self.state, target, reason=reason)
        )
        self.state = target


class CultureExperimentRunner:
    def __init__(
        self,
        *,
        controller: CultureEnvironmentController,
        sensor: ReadOnlySensor,
        clock: CultureClock,
        policy: CulturePolicy,
        qc_config: CultureQCConfig | None = None,
        compiler: CultureProtocolCompiler | None = None,
    ) -> None:
        self.controller = controller
        self.sensor = sensor
        self.clock = clock
        self.policy = policy
        self.qc_config = qc_config or CultureQCConfig()
        self.compiler = compiler or CultureProtocolCompiler()
        self.validator = CultureValidationService(self.compiler)

    async def run(
        self, proposal: ExperimentProposal, design: CultureDesign
    ) -> CultureExperimentRecord:
        audit = _Audit(proposal.experiment_id)

        def record(disposition: Disposition, **fields: object) -> CultureExperimentRecord:
            return CultureExperimentRecord.model_validate(
                {
                    "experiment_id": proposal.experiment_id,
                    "proposal": proposal,
                    "disposition": disposition,
                    "transitions": tuple(audit.transitions),
                    **fields,
                },
                strict=False,
            )

        # 1. Compile and validate. Nothing touches hardware until a receipt exists.
        try:
            protocol = self.compiler.compile(proposal, design)
        except CultureCompilationError as exc:
            audit.to(ExperimentState.REJECTED, "uncompilable proposal")
            return record("rejected", reason=str(exc))
        capabilities = await self.controller.capability_manifest()
        validation = self.validator.validate(
            proposal=proposal,
            protocol=protocol,
            design=design,
            policy=self.policy,
            capabilities=capabilities,
        )
        if validation.executable is None:
            audit.to(ExperimentState.REJECTED, "validation rejected the protocol")
            return record(
                "rejected",
                reason=", ".join(issue.code for issue in validation.report.issues),
                protocol=protocol,
                validation_report=validation.report,
            )
        audit.to(ExperimentState.VALIDATED)
        audit.to(ExperimentState.QUEUED)
        receipt = validation.executable
        common = {"protocol": protocol, "validation_report": validation.report}

        # 2. The receipt is only good for the device state it was issued against.
        stale = await self._stale_receipt(receipt)
        if stale is not None:
            audit.to(ExperimentState.FAILED, stale)
            return record("failed", reason=stale, **common)

        # 3. The sensor must be fit to read before any setpoint is applied.
        try:
            await self.sensor.open()
            self_test = await self.sensor.self_test()
            sensor_capabilities = await self.sensor.capability_manifest()
        except SensorError as exc:
            await self._close_sensor()
            audit.to(ExperimentState.FAILED, exc.code)
            return record("failed", reason=f"sensor_not_ready: {exc.code}", **common)
        if self_test.status is not SelfTestStatus.PASSED:
            await self._close_sensor()
            audit.to(ExperimentState.FAILED, "sensor self-test failed")
            return record("failed", reason="sensor_self_test_failed", **common)

        # 4. Execute. Safe state and sensor close happen on every exit path.
        audit.to(ExperimentState.RUNNING)
        evidence = await self._execute(receipt, design, sensor_capabilities)
        if evidence.acknowledgement is None or not evidence.acknowledgement.accepted:
            reason = (
                evidence.events[0].code
                if evidence.events
                else (evidence.acknowledgement.rejection_code if evidence.acknowledgement else None)
            )
            audit.to(ExperimentState.FAILED, reason)
            return record("failed", reason=reason, evidence=evidence, **common)
        if any(event.ambiguous for event in evidence.events):
            audit.to(ExperimentState.FAILED, "ambiguous device state")
            return record("failed", reason="ambiguous_device_state", evidence=evidence, **common)
        audit.to(ExperimentState.MEASURED)

        # 5. Reconcile -> QC -> analysis. QC failures are terminal, not learned from.
        reconciliation = reconcile_time_series(
            protocol.measurement_plan,
            hold_started_at=evidence.hold_started_at,
            readings=evidence.readings,
        )
        qc_report = evaluate_culture_qc(evidence, reconciliation, self.qc_config)
        measured = {**common, "evidence": evidence, "reconciliation": reconciliation}
        if not qc_report.passed:
            audit.to(ExperimentState.QC_FAILED, ", ".join(qc_report.failed_codes))
            return record(
                "qc_failed",
                reason=", ".join(qc_report.failed_codes),
                qc_report=qc_report,
                **measured,
            )
        audit.to(ExperimentState.QC_PASSED)
        metrics = analyse_culture(evidence, reconciliation)
        audit.to(ExperimentState.ANALYSED)
        return record("analysed", qc_report=qc_report, metrics=metrics, **measured)

    async def _stale_receipt(self, receipt: ExecutableCultureProtocol) -> str | None:
        live = await self.controller.capability_manifest()
        if live.sha256 != receipt.capability_snapshot_hash:
            return "receipt_stale: controller capabilities changed after validation"
        if self.policy.sha256 != receipt.validation_policy_hash:
            return "receipt_stale: validation policy changed"
        if receipt.protocol.compute_hash() != receipt.protocol_hash:
            return "receipt_stale: protocol changed"
        return None

    async def _execute(
        self,
        receipt: ExecutableCultureProtocol,
        design: CultureDesign,
        sensor_capabilities: SensorCapabilities,
    ) -> CultureRunEvidence:
        protocol = receipt.protocol
        hold = protocol.hold
        run_id = uuid5(NAMESPACE_URL, f"autolab:culture-run:{protocol.experiment_id}")
        command = SetpointCommand(
            experiment_id=protocol.experiment_id,
            run_id=run_id,
            reactor_id=hold.reactor_id,
            temperature_setpoint=hold.temperature_setpoint,
            light_intensity=hold.light_intensity,
            protocol_hash=receipt.protocol_hash,
            issued_at=self.clock.now(),
        )
        events: list[RunEvent] = []
        telemetry: list[EnvironmentTelemetry] = []
        readings: list[SampleReading] = []
        acknowledgement: SetpointAcknowledgement | None = None
        safe_state: SafeStateAcknowledgement | None = None
        stopped_early = False
        started_at = self.clock.now()
        safe_reason = "hold complete"
        try:
            try:
                acknowledgement = await self.controller.apply_setpoints(command)
            except ControllerError as exc:
                events.append(self._event(exc.code, str(exc), ambiguous=exc.ambiguous))
                safe_reason = f"setpoint command failed: {exc.code}"
            if acknowledgement is not None and acknowledgement.accepted:
                started_at = self.clock.now()
                stopped_early = await self._hold(
                    protocol, design, run_id, started_at, telemetry, readings, events
                )
                if stopped_early:
                    safe_reason = "device protection tripped"
            elif acknowledgement is not None:
                safe_reason = f"setpoint rejected: {acknowledgement.rejection_code}"
        except BaseException as exc:
            # Includes cancellation: record it, still reach safe state, then re-raise.
            events.append(self._event("execution_interrupted", repr(exc)))
            safe_reason = "execution interrupted"
            raise
        finally:
            safe_state = await self._safe_state(hold.reactor_id, safe_reason, events)
            await self._close_sensor()
        return CultureRunEvidence(
            run_id=run_id,
            experiment_id=protocol.experiment_id,
            receipt=receipt,
            controller_capability_sha256=receipt.capability_snapshot_hash,
            sensor_capabilities=sensor_capabilities,
            setpoint_command=command,
            acknowledgement=acknowledgement,
            hold_started_at=started_at,
            ended_at=self.clock.now(),
            telemetry=tuple(telemetry),
            readings=tuple(readings),
            events=tuple(events),
            stopped_early=stopped_early,
            safe_state=safe_state,
        )

    async def _hold(
        self,
        protocol: CultureProtocol,
        design: CultureDesign,
        run_id: UUID,
        started_at: datetime,
        telemetry: list[EnvironmentTelemetry],
        readings: list[SampleReading],
        events: list[RunEvent],
    ) -> bool:
        """Run the sampling schedule. Returns True if the hold stopped early."""

        for when, poll_telemetry, points in _schedule(protocol, design, started_at):
            await self.clock.sleep_until(when)
            if poll_telemetry:
                try:
                    sample = await self.controller.read_telemetry(protocol.hold.reactor_id)
                except ControllerError as exc:
                    # Telemetry is read-only; a failed read is a gap, not a hazard.
                    events.append(self._event(exc.code, str(exc)))
                else:
                    telemetry.append(sample)
                    if sample.protection_tripped:
                        events.append(
                            self._event("protection_tripped", "device protection tripped")
                        )
                        return True
            for point in points:
                readings.append(await self._read(protocol, run_id, point))
        return False

    async def _read(
        self, protocol: CultureProtocol, run_id: UUID, point: SamplePoint
    ) -> SampleReading:
        request = MeasurementRequest(
            experiment_id=protocol.experiment_id,
            run_id=run_id,
            sample_id=protocol.measurement_plan.sample_id,
            measurement_type=point.measurement_type,
            expected_unit=point.unit,
            wavelength=point.wavelength,
            requested_at=self.clock.now(),
            metadata={"expected_measurement_id": point.expected_measurement_id},
        )
        try:
            measurement = await asyncio.wait_for(
                self.sensor.read(request), timeout=float(request.timeout_seconds)
            )
        except SensorError as exc:
            return SampleReading(
                expected_measurement_id=point.expected_measurement_id,
                request=request,
                error_code=exc.code,
            )
        except TimeoutError:
            return SampleReading(
                expected_measurement_id=point.expected_measurement_id,
                request=request,
                error_code="sensor_timeout",
            )
        if measurement.request_id != request.request_id:
            return SampleReading(
                expected_measurement_id=point.expected_measurement_id,
                request=request,
                error_code="sensor_response_request_mismatch",
            )
        return SampleReading(
            expected_measurement_id=point.expected_measurement_id,
            request=request,
            measurement=measurement,
        )

    async def _safe_state(
        self, reactor_id: str, reason: str, events: list[RunEvent]
    ) -> SafeStateAcknowledgement | None:
        try:
            acknowledgement = await self.controller.enter_safe_state(reactor_id, reason)
        except ControllerError as exc:
            events.append(
                self._event(exc.code, f"safe state request failed: {exc}", ambiguous=True)
            )
            return None
        if not acknowledgement.reached:
            events.append(
                self._event("safe_state_not_reached", acknowledgement.detail or "", ambiguous=True)
            )
        return acknowledgement

    async def _close_sensor(self) -> None:
        if self.sensor.is_open:
            # A read-only sensor that fails to close cannot harm the culture.
            with contextlib.suppress(SensorError):
                await self.sensor.close()

    def _event(self, code: str, message: str, *, ambiguous: bool = False) -> RunEvent:
        return RunEvent(at=self.clock.now(), code=code, message=message, ambiguous=ambiguous)


def _schedule(
    protocol: CultureProtocol, design: CultureDesign, started_at: datetime
) -> list[tuple[datetime, bool, tuple[SamplePoint, ...]]]:
    """Merge telemetry polls and sample points into one ordered list of wake-ups.

    Telemetry is polled at the start, every ``telemetry_interval`` and at the end,
    so energy use always has both end points.
    """

    end = started_at + to_timedelta(protocol.hold.duration)
    step = to_timedelta(design.telemetry_interval)
    telemetry_times: set[datetime] = {started_at, end}
    current = started_at
    while current + step < end:
        current += step
        telemetry_times.add(current)
    points_at: dict[datetime, list[SamplePoint]] = {}
    for point in protocol.measurement_plan.points:
        points_at.setdefault(point.due_at(started_at), []).append(point)
    times = sorted(telemetry_times | set(points_at))
    return [(when, when in telemetry_times, tuple(points_at.get(when, ()))) for when in times]
