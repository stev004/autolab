"""Run one culture experiment end to end: propose -> validate -> execute -> measure -> QC.

The runner is the only place that joins the culture pieces. It never chooses
conditions (the proposal does), never decides whether a command is safe (the
validator and the device do), and never interprets data (QC and analysis do).

Execution order for one experiment::

    compile -> validate (receipt) -> re-check receipt against the live device
      -> open + self-test sensor -> journal "dispatching"
      -> apply setpoints to every replicate reactor (each acknowledged)
      -> until the hold ends: telemetry every telemetry_interval per reactor,
                              one OD reading per reactor per plan entry
      -> request safe state (per reactor, on every exit path*), close sensor
      -> reconcile time series -> per-replicate QC -> replicate rule -> analysis

\\* Except a deliberate detach: with ``on_interrupt="keep_holding"`` a cancelled
run (Ctrl-C, process shutdown) leaves the device holding under its own local
control and protection, and ``resume`` picks the hold up later.

Long-running state: every command, acknowledgement, telemetry sample, reading
and safe-state result is passed to an optional ``CultureRunJournal`` as it
happens. After a restart, ``resume`` rebuilds the run from that journal, checks
the device is still holding the same command, and continues the schedule.
Readings whose window closed during the outage are recorded as missing.

Every experiment-state change goes through AutoLab's domain state machine, so
the audit trail uses the same legal transitions as assay experiments. The runner
has no storage; persistence lives in ``campaigns/culture_service.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol
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
from autonomous_lab.culture.evidence import CultureRunEvidence, ReplicateEvidence, RunEvent
from autonomous_lab.culture.models import (
    CultureControllerCapabilities,
    CultureDesign,
    CulturePolicy,
    CultureProtocol,
    CultureQCConfig,
    CultureReplicate,
    ExecutableCultureProtocol,
    SamplePoint,
    to_timedelta,
)
from autonomous_lab.culture.qc import CultureExperimentQC, evaluate_culture_qc
from autonomous_lab.culture.timeseries import (
    SampleReading,
    TimeSeriesReconciliation,
    reconcile_time_series,
)
from autonomous_lab.culture.validation import CultureValidationResult, CultureValidationService
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
InterruptPolicy = Literal["safe_state", "keep_holding"]
JournalKind = Literal[
    "setpoint_command",
    "setpoint_ack",
    "hold_started",
    "telemetry",
    "reading",
    "safe_state",
    "sensor_session",
    "event",
]


class CultureResumeError(RuntimeError):
    """An interrupted culture cannot be resumed or closed automatically.

    Raised when the device cannot be reached to confirm its state. An operator
    must check the reactor and reconcile the experiment.
    """


class CultureExperimentRecord(DomainModel):
    """The complete, hashable evidence trail for one culture experiment."""

    schema_version: Literal["2"] = "2"
    experiment_id: UUID
    proposal: ExperimentProposal
    disposition: Disposition
    reason: str | None = None
    protocol: CultureProtocol | None = None
    validation_report: ValidationReport | None = None
    evidence: CultureRunEvidence | None = None
    reconciliations: tuple[TimeSeriesReconciliation, ...] = ()
    qc_report: CultureExperimentQC | None = None
    metrics: CultureMetrics | None = None
    transitions: tuple[StateTransition, ...] = Field(default=())

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)

    @property
    def reconciliation(self) -> TimeSeriesReconciliation | None:
        """The first replicate's reconciliation (convenient for one reactor)."""

        return self.reconciliations[0] if self.reconciliations else None


class DispatchHeader(DomainModel):
    """Journalled before the first setpoint, so a restart can rebuild the run."""

    run_id: UUID
    experiment_id: UUID
    receipt: ExecutableCultureProtocol
    #: The manifest the receipt was validated against (its hash is in the receipt).
    controller_capabilities: CultureControllerCapabilities
    sensor_capabilities: SensorCapabilities
    #: One planned command per replicate, in replicate order.
    commands: tuple[SetpointCommand, ...]


class HoldStarted(DomainModel):
    at: datetime


class InterruptedRun(DomainModel):
    """A run rebuilt from its journal after AutoLab stopped mid-experiment."""

    protocol: CultureProtocol
    validation_report: ValidationReport
    header: DispatchHeader
    hold_started_at: datetime | None
    replicates: tuple[ReplicateEvidence, ...]
    #: Reactors whose setpoint command was journalled as about to be sent.
    sent_reactors: frozenset[str] = frozenset()
    events: tuple[RunEvent, ...] = ()
    resumed_at: tuple[datetime, ...] = ()
    resumed_sensor_capabilities: tuple[SensorCapabilities, ...] = ()


class CultureRunObserver(Protocol):
    """Hooks so a caller can make pre-dispatch facts durable as they happen."""

    def on_validated(
        self,
        result: CultureValidationResult,
        capabilities: CultureControllerCapabilities,
    ) -> None: ...

    def on_transition(self, transition: StateTransition) -> None: ...


class CultureRunJournal(Protocol):
    """Append-only record of execution facts, written as they happen."""

    def dispatching(self, header: DispatchHeader) -> None: ...

    def record(
        self, kind: JournalKind, payload: DomainModel, *, reactor_id: str | None
    ) -> None: ...


class _Audit:
    """Track experiment state through the shared domain state machine."""

    def __init__(
        self,
        experiment_id: UUID,
        observer: CultureRunObserver | None,
        start: ExperimentState = ExperimentState.PROPOSED,
    ) -> None:
        self.experiment_id = experiment_id
        self.state = start
        self.transitions: list[StateTransition] = []
        self.observer = observer

    def to(self, target: ExperimentState, reason: str | None = None) -> None:
        transition = transition_experiment(self.experiment_id, self.state, target, reason=reason)
        self.transitions.append(transition)
        self.state = target
        if self.observer is not None:
            self.observer.on_transition(transition)


@dataclass
class _Replicate:
    """Mutable per-reactor state while the hold runs; frozen into evidence at the end."""

    spec: CultureReplicate
    command: SetpointCommand
    acknowledgement: SetpointAcknowledgement | None = None
    telemetry: list[EnvironmentTelemetry] = field(
        default_factory=lambda: list[EnvironmentTelemetry]()
    )
    readings: list[SampleReading] = field(default_factory=lambda: list[SampleReading]())
    stopped_early: bool = False
    hold_interrupted: bool = False
    safe_state: SafeStateAcknowledgement | None = None
    safe_requested: bool = False
    #: The command may have reached the device (journalled just before sending).
    sent: bool = False

    @property
    def reactor_id(self) -> str:
        return self.spec.hold.reactor_id

    @property
    def active(self) -> bool:
        accepted = self.acknowledgement is not None and self.acknowledgement.accepted
        return accepted and not self.stopped_early and not self.safe_requested

    def freeze(self) -> ReplicateEvidence:
        return ReplicateEvidence(
            replicate_index=self.spec.replicate_index,
            reactor_id=self.reactor_id,
            setpoint_command=self.command,
            acknowledgement=self.acknowledgement,
            telemetry=tuple(self.telemetry),
            readings=tuple(self.readings),
            stopped_early=self.stopped_early,
            hold_interrupted=self.hold_interrupted,
            safe_state=self.safe_state,
        )


class _NullJournal:
    def dispatching(self, header: DispatchHeader) -> None:
        del header

    def record(self, kind: JournalKind, payload: DomainModel, *, reactor_id: str | None) -> None:
        del kind, payload, reactor_id


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
        on_interrupt: InterruptPolicy = "safe_state",
    ) -> None:
        self.controller = controller
        self.sensor = sensor
        self.clock = clock
        self.policy = policy
        self.qc_config = qc_config or CultureQCConfig()
        self.compiler = compiler or CultureProtocolCompiler()
        self.validator = CultureValidationService(self.compiler)
        self.on_interrupt = on_interrupt

    # ------------------------------------------------------------------ run

    async def run(
        self,
        proposal: ExperimentProposal,
        design: CultureDesign,
        *,
        observer: CultureRunObserver | None = None,
        journal: CultureRunJournal | None = None,
    ) -> CultureExperimentRecord:
        audit = _Audit(proposal.experiment_id, observer)
        journal = journal or _NullJournal()

        def record(disposition: Disposition, **fields: object) -> CultureExperimentRecord:
            return _record(proposal, audit, disposition, **fields)

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
        if observer is not None:
            observer.on_validated(validation, capabilities)
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
        not_ready = await self._ready_sensor()
        if isinstance(not_ready, str):
            audit.to(ExperimentState.FAILED, not_ready)
            return record("failed", reason=not_ready, **common)
        sensor_capabilities = not_ready

        # 4. Execute. Safe state and sensor close happen on every exit path.
        audit.to(ExperimentState.RUNNING)
        header = DispatchHeader(
            run_id=uuid5(NAMESPACE_URL, f"autolab:culture-run:{protocol.experiment_id}"),
            experiment_id=protocol.experiment_id,
            receipt=receipt,
            controller_capabilities=capabilities,
            sensor_capabilities=sensor_capabilities,
            commands=(),
        )
        run_id = uuid5(NAMESPACE_URL, f"autolab:culture-run:{protocol.experiment_id}")
        commands = tuple(self._command(run_id, receipt, spec) for spec in protocol.replicates)
        header = header.model_copy(update={"commands": commands})
        journal.dispatching(header)
        replicates = [
            _Replicate(spec=spec, command=command)
            for spec, command in zip(protocol.replicates, commands, strict=True)
        ]
        events: list[RunEvent] = []
        evidence = await self._execute(
            header, replicates, events, journal, hold_started_at=None, resumed_at=()
        )
        return self._conclude(proposal, audit, protocol, validation.report, evidence)

    # --------------------------------------------------------------- resume

    async def resume(
        self,
        proposal: ExperimentProposal,
        interrupted: InterruptedRun,
        *,
        observer: CultureRunObserver | None = None,
        journal: CultureRunJournal | None = None,
    ) -> CultureExperimentRecord:
        """Continue (or safely close) a run that AutoLab stopped mid-experiment."""

        audit = _Audit(proposal.experiment_id, observer, start=ExperimentState.RUNNING)
        journal = journal or _NullJournal()
        header = interrupted.header
        events = list(interrupted.events)
        now = self.clock.now()
        events.append(RunEvent(at=now, code="resumed", message="AutoLab resumed the run"))
        journal.record("event", events[-1], reactor_id=None)
        replicates = [
            _thaw(spec, item, sent=item.reactor_id in interrupted.sent_reactors)
            for spec, item in _pair(interrupted)
        ]

        if interrupted.hold_started_at is None:
            if not any(item.sent for item in replicates):
                audit.to(ExperimentState.FAILED, "interrupted before any setpoint was sent")
                return _record(
                    proposal,
                    audit,
                    "failed",
                    reason="interrupted_before_dispatch",
                    protocol=interrupted.protocol,
                    validation_report=interrupted.validation_report,
                )
            # A setpoint may or may not have reached the device: never guess.
            events.append(
                self._event(
                    "ambiguous_setpoint_dispatch",
                    "AutoLab stopped while setpoints were being applied",
                    ambiguous=True,
                )
            )
            await self._safe_all(replicates, "resume after interrupted dispatch", events, journal)
            if any(item.safe_state is None or not item.safe_state.reached for item in replicates):
                raise CultureResumeError(
                    "could not confirm safe state after an interrupted dispatch; "
                    "check the reactors and reconcile the experiment"
                )
            await self._close_sensor()
            evidence = self._freeze(header, replicates, events, now, (*interrupted.resumed_at, now))
            return self._conclude(
                proposal, audit, interrupted.protocol, interrupted.validation_report, evidence
            )

        unfinished = [item for item in replicates if item.active]
        if unfinished:
            # Confirm each reactor is still holding *our* command before trusting it.
            for item in unfinished:
                try:
                    sample = await self.controller.read_telemetry(item.reactor_id)
                except ControllerError as exc:
                    raise CultureResumeError(
                        f"cannot reach controller to confirm {item.reactor_id} ({exc.code}); "
                        "check the reactor and reconcile the experiment"
                    ) from exc
                item.telemetry.append(sample)
                journal.record("telemetry", sample, reactor_id=item.reactor_id)
                assert item.acknowledgement is not None
                if (
                    sample.active_command_id != item.acknowledgement.command_id
                    or sample.protection_tripped
                ):
                    item.hold_interrupted = True
                    item.stopped_early = True
                    events.append(
                        self._event(
                            "hold_not_confirmed",
                            "device is not holding the acknowledged command",
                            reactor_id=item.reactor_id,
                        )
                    )
                    journal.record("event", events[-1], reactor_id=item.reactor_id)
                    await self._safe(item, "hold not confirmed after restart", events, journal)
        sensors = list(interrupted.resumed_sensor_capabilities)
        if any(item.active for item in replicates):
            ready = await self._ready_sensor()
            if not isinstance(ready, str):
                sensors.append(ready)
                journal.record("sensor_session", ready, reactor_id=None)
            if isinstance(ready, str):
                events.append(self._event(ready, "sensor not ready after restart"))
                await self._safe_all(replicates, "sensor not ready after restart", events, journal)
                evidence = self._freeze(
                    header,
                    replicates,
                    events,
                    interrupted.hold_started_at,
                    (*interrupted.resumed_at, now),
                    tuple(sensors),
                )
                audit.to(ExperimentState.FAILED, ready)
                return _record(
                    proposal,
                    audit,
                    "failed",
                    reason=ready,
                    protocol=interrupted.protocol,
                    validation_report=interrupted.validation_report,
                    evidence=evidence,
                )
        evidence = await self._execute(
            header,
            replicates,
            events,
            journal,
            hold_started_at=interrupted.hold_started_at,
            resumed_at=(*interrupted.resumed_at, now),
            resumed_sensors=tuple(sensors),
        )
        return self._conclude(
            proposal, audit, interrupted.protocol, interrupted.validation_report, evidence
        )

    # ------------------------------------------------------------ execution

    async def _execute(
        self,
        header: DispatchHeader,
        replicates: list[_Replicate],
        events: list[RunEvent],
        journal: CultureRunJournal,
        *,
        hold_started_at: datetime | None,
        resumed_at: tuple[datetime, ...],
        resumed_sensors: tuple[SensorCapabilities, ...] = (),
    ) -> CultureRunEvidence:
        detached = False
        started_at = hold_started_at or self.clock.now()
        safe_reason = "hold complete"
        try:
            applied = True
            if hold_started_at is None:
                applied = await self._apply_all(replicates, events, journal)
                if applied:
                    started_at = self.clock.now()
                    journal.record("hold_started", HoldStarted(at=started_at), reactor_id=None)
                else:
                    safe_reason = "setpoints not applied on every replicate"
            if applied:
                await self._hold(header.receipt.protocol, started_at, replicates, events, journal)
        except (asyncio.CancelledError, KeyboardInterrupt):
            if self.on_interrupt == "keep_holding" and any(item.active for item in replicates):
                detached = True
                events.append(
                    self._event("detached", "AutoLab stopped; device keeps holding locally")
                )
                journal.record("event", events[-1], reactor_id=None)
            else:
                events.append(self._event("execution_interrupted", "run was cancelled"))
                journal.record("event", events[-1], reactor_id=None)
                safe_reason = "execution interrupted"
            raise
        except BaseException as exc:
            events.append(self._event("execution_interrupted", repr(exc)))
            journal.record("event", events[-1], reactor_id=None)
            safe_reason = "execution interrupted"
            raise
        finally:
            if not detached:
                await self._safe_all(replicates, safe_reason, events, journal)
            await self._close_sensor()
        return self._freeze(header, replicates, events, started_at, resumed_at, resumed_sensors)

    async def _apply_all(
        self, replicates: list[_Replicate], events: list[RunEvent], journal: CultureRunJournal
    ) -> bool:
        """Apply setpoints reactor by reactor; stop at the first refusal or error."""

        for item in replicates:
            journal.record("setpoint_command", item.command, reactor_id=item.reactor_id)
            item.sent = True
            try:
                ack = await self.controller.apply_setpoints(item.command)
            except ControllerError as exc:
                events.append(
                    self._event(
                        exc.code, str(exc), ambiguous=exc.ambiguous, reactor_id=item.reactor_id
                    )
                )
                journal.record("event", events[-1], reactor_id=item.reactor_id)
                return False
            item.acknowledgement = ack
            journal.record("setpoint_ack", ack, reactor_id=item.reactor_id)
            if not ack.accepted:
                events.append(
                    self._event(
                        ack.rejection_code or "setpoint_rejected",
                        ack.rejection_message or "device refused the setpoint",
                        reactor_id=item.reactor_id,
                    )
                )
                journal.record("event", events[-1], reactor_id=item.reactor_id)
                return False
        return True

    async def _hold(
        self,
        protocol: CultureProtocol,
        started_at: datetime,
        replicates: list[_Replicate],
        events: list[RunEvent],
        journal: CultureRunJournal,
    ) -> None:
        """Run the shared schedule for every active replicate.

        On resume, wake-ups already in the past are skipped, except readings whose
        tolerance window is still open; those are taken now and still count.
        """

        now = self.clock.now()
        done = {
            (item.reactor_id, reading.expected_measurement_id)
            for item in replicates
            for reading in item.readings
        }
        for when, poll_telemetry, points in _schedule(protocol, started_at):
            if not any(item.active for item in replicates):
                return
            late = when < now
            await self.clock.sleep_until(when)
            for item in replicates:
                if not item.active:
                    continue
                if poll_telemetry and not late:
                    await self._poll(item, events, journal)
                    if not item.active:
                        continue
                for point in points.get(item.reactor_id, ()):
                    if (item.reactor_id, point.expected_measurement_id) in done:
                        continue
                    if late and point.window(started_at)[1] < self.clock.now():
                        continue  # window closed during an outage: recorded as missing
                    reading = await self._read(protocol, item, point)
                    item.readings.append(reading)
                    journal.record("reading", reading, reactor_id=item.reactor_id)

    async def _poll(
        self, item: _Replicate, events: list[RunEvent], journal: CultureRunJournal
    ) -> None:
        try:
            sample = await self.controller.read_telemetry(item.reactor_id)
        except ControllerError as exc:
            # Telemetry is read-only; a failed read is a gap, not a hazard.
            events.append(self._event(exc.code, str(exc), reactor_id=item.reactor_id))
            journal.record("event", events[-1], reactor_id=item.reactor_id)
            return
        item.telemetry.append(sample)
        journal.record("telemetry", sample, reactor_id=item.reactor_id)
        if sample.protection_tripped:
            item.stopped_early = True
            events.append(
                self._event(
                    "protection_tripped", "device protection tripped", reactor_id=item.reactor_id
                )
            )
            journal.record("event", events[-1], reactor_id=item.reactor_id)
            await self._safe(item, "device protection tripped", events, journal)

    async def _read(
        self, protocol: CultureProtocol, item: _Replicate, point: SamplePoint
    ) -> SampleReading:
        request = MeasurementRequest(
            experiment_id=protocol.experiment_id,
            run_id=item.command.run_id,
            sample_id=item.spec.measurement_plan.sample_id,
            measurement_type=point.measurement_type,
            expected_unit=point.unit,
            wavelength=point.wavelength,
            requested_at=self.clock.now(),
            metadata={"expected_measurement_id": point.expected_measurement_id},
        )

        def failed(code: str) -> SampleReading:
            return SampleReading(
                expected_measurement_id=point.expected_measurement_id,
                request=request,
                error_code=code,
            )

        try:
            measurement = await asyncio.wait_for(
                self.sensor.read(request), timeout=float(request.timeout_seconds)
            )
        except SensorError as exc:
            return failed(exc.code)
        except TimeoutError:
            return failed("sensor_timeout")
        if measurement.request_id != request.request_id:
            return failed("sensor_response_request_mismatch")
        if measurement.sample_id != request.sample_id:
            return failed("sensor_response_sample_mismatch")
        return SampleReading(
            expected_measurement_id=point.expected_measurement_id,
            request=request,
            measurement=measurement,
        )

    # ----------------------------------------------------------- safe state

    async def _safe_all(
        self,
        replicates: list[_Replicate],
        reason: str,
        events: list[RunEvent],
        journal: CultureRunJournal,
    ) -> None:
        for item in replicates:
            if item.sent and not item.safe_requested:
                await self._safe(item, reason, events, journal)

    async def _safe(
        self,
        item: _Replicate,
        reason: str,
        events: list[RunEvent],
        journal: CultureRunJournal,
    ) -> None:
        item.safe_requested = True
        try:
            acknowledgement = await self.controller.enter_safe_state(item.reactor_id, reason)
        except ControllerError as exc:
            events.append(
                self._event(
                    exc.code,
                    f"safe state request failed: {exc}",
                    ambiguous=True,
                    reactor_id=item.reactor_id,
                )
            )
            journal.record("event", events[-1], reactor_id=item.reactor_id)
            return
        item.safe_state = acknowledgement
        journal.record("safe_state", acknowledgement, reactor_id=item.reactor_id)
        if not acknowledgement.reached:
            events.append(
                self._event(
                    "safe_state_not_reached",
                    acknowledgement.detail or "",
                    ambiguous=True,
                    reactor_id=item.reactor_id,
                )
            )
            journal.record("event", events[-1], reactor_id=item.reactor_id)

    # --------------------------------------------------------- conclusion

    def _conclude(
        self,
        proposal: ExperimentProposal,
        audit: _Audit,
        protocol: CultureProtocol,
        report: ValidationReport,
        evidence: CultureRunEvidence,
    ) -> CultureExperimentRecord:
        common: dict[str, object] = {
            "protocol": protocol,
            "validation_report": report,
            "evidence": evidence,
        }
        refused = [item for item in evidence.replicates if not item.accepted]
        if refused:
            reason = next(
                (event.code for event in evidence.events if event.code not in {"resumed"}),
                "setpoint_not_applied",
            )
            audit.to(ExperimentState.FAILED, reason)
            return _record(proposal, audit, "failed", reason=reason, **common)
        if any(event.ambiguous for event in evidence.events):
            audit.to(ExperimentState.FAILED, "ambiguous device state")
            return _record(proposal, audit, "failed", reason="ambiguous_device_state", **common)
        audit.to(ExperimentState.MEASURED)

        reconciliations = {
            spec.hold.reactor_id: reconcile_time_series(
                spec.measurement_plan,
                hold_started_at=evidence.hold_started_at,
                readings=evidence.replicate(spec.hold.reactor_id).readings,
            )
            for spec in protocol.replicates
        }
        qc = evaluate_culture_qc(
            evidence,
            reconciliations,
            self.qc_config,
            minimum_passing_replicates=protocol.minimum_passing_replicates,
        )
        measured = {**common, "reconciliations": tuple(reconciliations.values())}
        if not qc.passed:
            reason = ", ".join(qc.failed_codes)
            audit.to(ExperimentState.QC_FAILED, reason)
            return _record(proposal, audit, "qc_failed", reason=reason, qc_report=qc, **measured)
        audit.to(ExperimentState.QC_PASSED)
        metrics = analyse_culture(evidence, reconciliations, qc.passing_reactors)
        audit.to(ExperimentState.ANALYSED)
        return _record(proposal, audit, "analysed", qc_report=qc, metrics=metrics, **measured)

    # -------------------------------------------------------------- helpers

    def _command(
        self, run_id: UUID, receipt: ExecutableCultureProtocol, spec: CultureReplicate
    ) -> SetpointCommand:
        return SetpointCommand(
            command_id=uuid5(NAMESPACE_URL, f"{run_id}:{spec.hold.reactor_id}:setpoint"),
            experiment_id=receipt.protocol.experiment_id,
            run_id=run_id,
            reactor_id=spec.hold.reactor_id,
            setpoints=dict(spec.hold.setpoints),
            protocol_hash=receipt.protocol_hash,
            issued_at=self.clock.now(),
        )

    async def _stale_receipt(self, receipt: ExecutableCultureProtocol) -> str | None:
        live = await self.controller.capability_manifest()
        if live.sha256 != receipt.capability_snapshot_hash:
            return "receipt_stale: controller capabilities changed after validation"
        if self.policy.sha256 != receipt.validation_policy_hash:
            return "receipt_stale: validation policy changed"
        if receipt.protocol.compute_hash() != receipt.protocol_hash:
            return "receipt_stale: protocol changed"
        return None

    async def _ready_sensor(self) -> SensorCapabilities | str:
        """Open and self-test the sensor; return its capabilities or a failure reason."""

        try:
            await self.sensor.open()
            self_test = await self.sensor.self_test()
            capabilities = await self.sensor.capability_manifest()
        except SensorError as exc:
            await self._close_sensor()
            return f"sensor_not_ready: {exc.code}"
        if self_test.status is not SelfTestStatus.PASSED:
            await self._close_sensor()
            return "sensor_self_test_failed"
        return capabilities

    async def _close_sensor(self) -> None:
        if self.sensor.is_open:
            # A read-only sensor that fails to close cannot harm the culture.
            with contextlib.suppress(SensorError):
                await self.sensor.close()

    def _freeze(
        self,
        header: DispatchHeader,
        replicates: list[_Replicate],
        events: list[RunEvent],
        started_at: datetime,
        resumed_at: tuple[datetime, ...],
        resumed_sensors: tuple[SensorCapabilities, ...] = (),
    ) -> CultureRunEvidence:
        return CultureRunEvidence(
            run_id=header.run_id,
            experiment_id=header.experiment_id,
            receipt=header.receipt,
            controller_capability_sha256=header.receipt.capability_snapshot_hash,
            controller_capabilities=header.controller_capabilities,
            sensor_capabilities=header.sensor_capabilities,
            hold_started_at=started_at,
            ended_at=self.clock.now(),
            replicates=tuple(item.freeze() for item in replicates),
            events=tuple(events),
            resumed_at=resumed_at,
            resumed_sensor_capabilities=resumed_sensors,
        )

    def _event(
        self,
        code: str,
        message: str,
        *,
        ambiguous: bool = False,
        reactor_id: str | None = None,
    ) -> RunEvent:
        return RunEvent(
            at=self.clock.now(),
            code=code,
            message=message,
            ambiguous=ambiguous,
            reactor_id=reactor_id,
        )


def _record(
    proposal: ExperimentProposal, audit: _Audit, disposition: Disposition, **fields: object
) -> CultureExperimentRecord:
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


def _pair(interrupted: InterruptedRun) -> list[tuple[CultureReplicate, ReplicateEvidence]]:
    by_reactor = {item.reactor_id: item for item in interrupted.replicates}
    return [(spec, by_reactor[spec.hold.reactor_id]) for spec in interrupted.protocol.replicates]


def _thaw(spec: CultureReplicate, evidence: ReplicateEvidence, *, sent: bool) -> _Replicate:
    return _Replicate(
        spec=spec,
        command=evidence.setpoint_command,
        acknowledgement=evidence.acknowledgement,
        telemetry=list(evidence.telemetry),
        readings=list(evidence.readings),
        stopped_early=evidence.stopped_early,
        hold_interrupted=evidence.hold_interrupted,
        safe_state=evidence.safe_state,
        safe_requested=evidence.safe_state is not None,
        sent=sent or evidence.acknowledgement is not None,
    )


def _schedule(
    protocol: CultureProtocol, started_at: datetime
) -> list[tuple[datetime, bool, dict[str, tuple[SamplePoint, ...]]]]:
    """Merge telemetry polls and every replicate's sample points into ordered wake-ups.

    Telemetry is polled at the start, every ``telemetry_interval`` and at the end,
    so energy use always has both end points.
    """

    end = started_at + to_timedelta(protocol.duration)
    step = to_timedelta(protocol.telemetry_interval)
    telemetry_times: set[datetime] = {started_at, end}
    current = started_at
    while current + step < end:
        current += step
        telemetry_times.add(current)
    points_at: dict[datetime, dict[str, list[SamplePoint]]] = {}
    for spec in protocol.replicates:
        for point in spec.measurement_plan.points:
            points_at.setdefault(point.due_at(started_at), {}).setdefault(
                spec.hold.reactor_id, []
            ).append(point)
    times = sorted(telemetry_times | set(points_at))
    return [
        (
            when,
            when in telemetry_times,
            {reactor: tuple(points) for reactor, points in points_at.get(when, {}).items()},
        )
        for when in times
    ]
