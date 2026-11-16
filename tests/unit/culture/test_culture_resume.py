"""Resume after an interrupted dispatch, and the receipt and sensor identity guards.

Each test here was written to kill a mutant that survived the whole suite in an
external audit (named in the test docstring):

- M2: resume treats a possibly-sent setpoint as never sent, so it skips safe state;
- M4: the validation-policy staleness check on the receipt is removed;
- M6: the sensor response's request-identity check is removed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from uuid import uuid4

import pytest
from culture_fakes import (
    REACTORS,
    START,
    ScriptedController,
    ScriptedODSensor,
    design,
    policy,
    proposal,
)

from autonomous_lab.culture import (
    ControllerError,
    CultureControllerCapabilities,
    CultureExperimentRecord,
    CultureExperimentRunner,
    CultureResumeError,
    CultureValidationResult,
    DispatchHeader,
    InterruptedRun,
    ReplicateEvidence,
    SetpointAcknowledgement,
    SteppedClock,
)
from autonomous_lab.domain.base import DomainModel
from autonomous_lab.domain.experiments import ExperimentProposal
from autonomous_lab.domain.state_machines import StateTransition
from autonomous_lab.hardware.sensors import MeasurementRequest, RawSensorMeasurement


@dataclass
class _HeaderJournal:
    header: DispatchHeader | None = None
    kinds: list[str] = field(default_factory=list)

    def dispatching(self, header: DispatchHeader) -> None:
        self.header = header

    def record(self, kind: str, payload: DomainModel, *, reactor_id: str | None) -> None:
        del payload, reactor_id
        self.kinds.append(kind)


def _dispatched(prop: ExperimentProposal) -> tuple[CultureExperimentRecord, DispatchHeader]:
    """A real header and validation report for a three-reactor run of ``prop``."""

    clock = SteppedClock(START)
    journal = _HeaderJournal()
    runner = CultureExperimentRunner(
        controller=ScriptedController(clock=clock),
        sensor=ScriptedODSensor(clock=clock),
        clock=clock,
        policy=policy(),
    )
    record = asyncio.run(runner.run(prop, design(reactor_ids=REACTORS), journal=journal))
    assert journal.header is not None
    return record, journal.header


def _interrupted_during_dispatch(
    *, sent: frozenset[str], acknowledged: frozenset[str] = frozenset()
) -> tuple[ExperimentProposal, InterruptedRun]:
    """AutoLab died while applying setpoints: no hold start was journalled.

    ``sent`` are reactors whose ``setpoint_command`` was journalled just before
    sending; ``acknowledged`` also got their acknowledgement journalled.
    """

    prop = proposal()
    record, header = _dispatched(prop)
    assert record.protocol is not None and record.validation_report is not None
    replicates = []
    for spec, command in zip(record.protocol.replicates, header.commands, strict=True):
        ack = None
        if spec.hold.reactor_id in acknowledged:
            ack = SetpointAcknowledgement(
                command_id=command.command_id,
                controller_id="ctrl-1",
                reactor_id=spec.hold.reactor_id,
                accepted=True,
                applied=dict(command.setpoints),
                acknowledged_at=START,
            )
        replicates.append(
            ReplicateEvidence(
                replicate_index=spec.replicate_index,
                reactor_id=spec.hold.reactor_id,
                setpoint_command=command,
                acknowledgement=ack,
            )
        )
    return prop, InterruptedRun(
        protocol=record.protocol,
        validation_report=record.validation_report,
        header=header,
        hold_started_at=None,
        replicates=tuple(replicates),
        sent_reactors=sent,
    )


def _resume(
    prop: ExperimentProposal, interrupted: InterruptedRun, **controller: object
) -> tuple[CultureExperimentRecord, ScriptedController]:
    clock = SteppedClock(START)
    device = ScriptedController(clock=clock, **controller)  # type: ignore[arg-type]
    runner = CultureExperimentRunner(
        controller=device, sensor=ScriptedODSensor(clock=clock), clock=clock, policy=policy()
    )
    return asyncio.run(runner.resume(prop, interrupted)), device


# ----------------------------------------------- M2: interrupted dispatch


def test_a_possibly_sent_setpoint_is_made_safe_on_resume_never_assumed_unsent() -> None:
    """M2. reactor-2's command was journalled but never acknowledged: it may be live."""

    prop, interrupted = _interrupted_during_dispatch(
        sent=frozenset({"reactor-1", "reactor-2"}), acknowledged=frozenset({"reactor-1"})
    )
    record, device = _resume(prop, interrupted)

    assert sorted(device.safe_state_reactors) == ["reactor-1", "reactor-2"]
    assert device.safe_state_calls == ["resume after interrupted dispatch"] * 2
    assert device.commands == []  # resume never re-sends a setpoint
    assert record.disposition == "failed"
    assert record.evidence is not None
    ambiguous = [event for event in record.evidence.events if event.ambiguous]
    assert [event.code for event in ambiguous] == ["ambiguous_setpoint_dispatch"]
    assert [item.to_state for item in record.transitions] == ["failed"]


def test_an_acknowledged_reactor_counts_as_sent_even_without_its_command_row() -> None:
    """M2 variant: the acknowledgement alone proves the command reached the device."""

    prop, interrupted = _interrupted_during_dispatch(
        sent=frozenset(), acknowledged=frozenset({"reactor-1"})
    )
    record, device = _resume(prop, interrupted)
    assert device.safe_state_reactors == ["reactor-1"]
    assert record.disposition == "failed"


def test_resume_needs_an_operator_if_safe_state_is_not_confirmed() -> None:
    prop, interrupted = _interrupted_during_dispatch(sent=frozenset({"reactor-1"}))
    failure = ControllerError("link_down", "no route to controller")
    with pytest.raises(CultureResumeError, match="reconcile"):
        _resume(prop, interrupted, safe_state_failure=failure)


def test_nothing_sent_closes_the_run_without_touching_the_device() -> None:
    prop, interrupted = _interrupted_during_dispatch(sent=frozenset())
    record, device = _resume(prop, interrupted)
    assert record.disposition == "failed"
    assert record.reason == "interrupted_before_dispatch"
    assert device.safe_state_calls == [] and device.commands == []


# ------------------------------------------------- M4: policy staleness


class _PolicyChangesAfterValidation:
    """Swap the runner's policy between validation and the staleness re-check."""

    def __init__(self, runner: CultureExperimentRunner) -> None:
        self.runner = runner

    def on_validated(
        self, result: CultureValidationResult, capabilities: CultureControllerCapabilities
    ) -> None:
        del result, capabilities
        self.runner.policy = policy(maximum_sample_points=199)

    def on_transition(self, transition: StateTransition) -> None:
        del transition


def test_a_receipt_is_refused_if_the_validation_policy_changed() -> None:
    """M4."""

    clock = SteppedClock(START)
    device = ScriptedController(clock=clock)
    runner = CultureExperimentRunner(
        controller=device, sensor=ScriptedODSensor(clock=clock), clock=clock, policy=policy()
    )
    record = asyncio.run(
        runner.run(proposal(), design(), observer=_PolicyChangesAfterValidation(runner))
    )
    assert record.disposition == "failed"
    assert record.reason == "receipt_stale: validation policy changed"
    assert device.commands == []


# ------------------------------------------- M6: sensor response identity


@dataclass
class _ConfusedSensor(ScriptedODSensor):
    """Answers with another request's (or another sample's) measurement."""

    wrong: str = "request"

    async def read(self, request: MeasurementRequest) -> RawSensorMeasurement:
        measurement = await super().read(request)
        if self.wrong == "request":
            return measurement.model_copy(update={"request_id": uuid4()})
        return measurement.model_copy(update={"sample_id": "someone-else"})


@pytest.mark.parametrize(
    ("wrong", "code"),
    [
        ("request", "sensor_response_request_mismatch"),
        ("sample", "sensor_response_sample_mismatch"),
    ],
)
def test_a_reading_for_another_request_is_never_counted(wrong: str, code: str) -> None:
    """M6 (and its sample-identity sibling)."""

    clock = SteppedClock(START)
    runner = CultureExperimentRunner(
        controller=ScriptedController(clock=clock),
        sensor=_ConfusedSensor(clock=clock, wrong=wrong),
        clock=clock,
        policy=policy(),
    )
    record = asyncio.run(runner.run(proposal(), design()))
    assert record.evidence is not None
    readings = record.evidence.replicates[0].readings
    assert len(readings) == 4
    assert {reading.error_code for reading in readings} == {code}
    assert all(reading.measurement is None for reading in readings)
    assert record.disposition == "qc_failed"
