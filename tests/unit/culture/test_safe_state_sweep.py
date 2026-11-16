"""The safe-state sweep is fault-isolated and every controller call is bounded.

These reproduce the external audit's three probes on a three-reactor run, where a
fault on reactor-1 used to stop reactors 2 and 3 receiving ``enter_safe_state``:

1. reactor-1's safe-state call raises an ``OSError`` (not a ``ControllerError``);
2. the journal write of reactor-1's safe-state result fails;
3. reactor-1's safe-state call hangs.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

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
    CultureExperimentRecord,
    CultureExperimentRunner,
    DispatchHeader,
    SafeStateAcknowledgement,
    SteppedClock,
)
from autonomous_lab.culture.batch import SerialController
from autonomous_lab.domain.base import DomainModel

SafeBehaviour = Callable[[], Awaitable[None]]


@dataclass
class FaultyController(ScriptedController):
    """Scripted controller whose safe-state call misbehaves for chosen reactors."""

    safe_faults: dict[str, SafeBehaviour] = field(default_factory=dict)

    async def enter_safe_state(self, reactor_id: str, reason: str) -> SafeStateAcknowledgement:
        fault = self.safe_faults.get(reactor_id)
        if fault is not None:
            self.safe_state_reactors.append(reactor_id)
            await fault()
        return await super().enter_safe_state(reactor_id, reason)


@dataclass
class RecordingJournal:
    """In-memory journal; optionally fails the first ``safe_state`` write."""

    fail_safe_state_for: str | None = None
    entries: list[tuple[str, str | None]] = field(default_factory=list)

    def dispatching(self, header: DispatchHeader) -> None:
        del header

    def record(self, kind: str, payload: DomainModel, *, reactor_id: str | None) -> None:
        del payload
        if kind == "safe_state" and reactor_id == self.fail_safe_state_for:
            self.fail_safe_state_for = None
            raise OSError("disk full")
        self.entries.append((kind, reactor_id))


def _runner(controller: ScriptedController, clock: SteppedClock, **kwargs: object):
    return CultureExperimentRunner(
        controller=controller,
        sensor=ScriptedODSensor(clock=clock),
        clock=clock,
        policy=policy(),
        **kwargs,  # type: ignore[arg-type]
    )


def _three() -> object:
    return design(reactor_ids=REACTORS)


async def _raise_oserror() -> None:
    raise OSError("serial port vanished")


async def _hang() -> None:
    await asyncio.Event().wait()


def _safe_reactors(controller: ScriptedController) -> list[str]:
    return sorted(set(controller.safe_state_reactors))


def test_probe_1_any_fault_on_one_reactor_still_safes_the_others() -> None:
    clock = SteppedClock(START)
    controller = FaultyController(clock=clock, safe_faults={"reactor-1": _raise_oserror})
    record: CultureExperimentRecord = asyncio.run(
        _runner(controller, clock).run(proposal(), _three())  # type: ignore[arg-type]
    )
    assert _safe_reactors(controller) == list(REACTORS)
    assert record.disposition == "failed"
    assert record.reason == "ambiguous_device_state"
    assert record.evidence is not None
    codes = {(event.code, event.reactor_id) for event in record.evidence.events}
    assert ("safe_state_error", "reactor-1") in codes
    reached = {item.reactor_id for item in record.evidence.replicates if item.safe_state}
    assert reached == {"reactor-2", "reactor-3"}


def test_probe_2_a_journal_failure_still_safes_the_others_then_surfaces() -> None:
    clock = SteppedClock(START)
    controller = ScriptedController(clock=clock)
    journal = RecordingJournal(fail_safe_state_for="reactor-1")
    with pytest.raises(OSError, match="disk full"):
        asyncio.run(
            _runner(controller, clock).run(proposal(), _three(), journal=journal)  # type: ignore[arg-type]
        )
    assert sorted(controller.safe_state_reactors) == list(REACTORS)
    # The other reactors' results were still journalled.
    assert ("safe_state", "reactor-2") in journal.entries
    assert ("safe_state", "reactor-3") in journal.entries


def test_probe_3_a_hung_safe_state_call_is_bounded_and_the_others_are_safed() -> None:
    clock = SteppedClock(START)
    controller = FaultyController(clock=clock, safe_faults={"reactor-1": _hang})
    runner = _runner(controller, clock, safe_state_timeout_seconds=0.05)
    started = time.monotonic()
    record = asyncio.run(runner.run(proposal(), _three()))  # type: ignore[arg-type]
    assert time.monotonic() - started < 5
    assert _safe_reactors(controller) == list(REACTORS)
    assert record.reason == "ambiguous_device_state"
    assert record.evidence is not None
    assert any(
        event.code == "safe_state_timeout" and event.reactor_id == "reactor-1"
        for event in record.evidence.events
    )


def test_a_call_that_swallows_cancellation_cannot_hang_the_sweep() -> None:
    """``asyncio.wait_for`` would wait for this call to finish cancelling."""

    released = asyncio.Event()

    async def stubborn() -> None:
        while not released.is_set():
            try:
                await released.wait()
            except asyncio.CancelledError:
                continue

    async def main() -> CultureExperimentRecord:
        clock = SteppedClock(START)
        controller = FaultyController(clock=clock, safe_faults={"reactor-1": stubborn})
        try:
            return await _runner(controller, clock, safe_state_timeout_seconds=0.05).run(
                proposal(),
                _three(),  # type: ignore[arg-type]
            )
        finally:
            released.set()  # let the abandoned call end so the loop can close

    started = time.monotonic()
    record = asyncio.run(main())
    assert time.monotonic() - started < 5
    assert record.reason == "ambiguous_device_state"


def test_a_second_cancellation_during_the_sweep_does_not_cut_it_short() -> None:
    async def slow() -> None:
        await asyncio.sleep(0.05)

    clock = SteppedClock(START)
    controller = FaultyController(clock=clock, safe_faults={"reactor-1": slow})
    sensor = ScriptedODSensor(clock=clock)
    first_read = asyncio.Event()

    async def block_on_read(_request: object) -> object:
        first_read.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    sensor.read = block_on_read  # type: ignore[method-assign]
    runner = CultureExperimentRunner(
        controller=controller, sensor=sensor, clock=clock, policy=policy()
    )

    async def main() -> None:
        task = asyncio.ensure_future(runner.run(proposal(), _three()))  # type: ignore[arg-type]
        await first_read.wait()
        task.cancel()  # first Ctrl-C: the sweep starts
        await asyncio.sleep(0.01)
        task.cancel()  # second Ctrl-C while reactor-1 is still answering
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert _safe_reactors(controller) == list(REACTORS)


def test_a_hung_setpoint_is_an_ambiguous_timeout_and_is_made_safe() -> None:
    clock = SteppedClock(START)
    controller = ScriptedController(clock=clock)
    original = controller.apply_setpoints

    async def hang_on_reactor_2(command):  # type: ignore[no-untyped-def]
        if command.reactor_id == "reactor-2":
            controller.commands.append(command)
            await _hang()
        return await original(command)

    controller.apply_setpoints = hang_on_reactor_2  # type: ignore[method-assign]
    record = asyncio.run(
        _runner(controller, clock, controller_timeout_seconds=0.05).run(proposal(), _three())  # type: ignore[arg-type]
    )
    assert record.disposition == "failed"
    assert record.reason == "setpoint_timeout"
    assert record.evidence is not None
    timeout = next(event for event in record.evidence.events if event.code == "setpoint_timeout")
    assert timeout.ambiguous and timeout.reactor_id == "reactor-2"
    # reactor-1 was applied and reactor-2 may have been: both are made safe.
    assert sorted(controller.safe_state_reactors) == ["reactor-1", "reactor-2"]


def test_a_hung_telemetry_read_is_a_gap_not_a_stall() -> None:
    clock = SteppedClock(START)
    controller = ScriptedController(clock=clock)
    original = controller.read_telemetry
    calls = {"n": 0}

    async def hang_once(reactor_id: str):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 2:
            await _hang()
        return await original(reactor_id)

    controller.read_telemetry = hang_once  # type: ignore[method-assign]
    record = asyncio.run(
        _runner(controller, clock, controller_timeout_seconds=0.05).run(proposal(), design())  # type: ignore[arg-type]
    )
    assert record.evidence is not None
    assert [event.code for event in record.evidence.events] == ["telemetry_timeout"]
    assert record.evidence.replicates[0].safe_state is not None


def test_timeouts_must_be_positive() -> None:
    clock = SteppedClock(START)
    with pytest.raises(ValueError, match="positive"):
        _runner(ScriptedController(clock=clock), clock, safe_state_timeout_seconds=0)


# --------------------------------------------------------- batch serialisation


@dataclass
class _LockProbe:
    """A bare controller that can hang a call while holding SerialController's lock."""

    released: asyncio.Event
    in_flight: int = 0
    max_in_flight: int = 0
    safed: list[str] = field(default_factory=list)

    @property
    def controller_id(self) -> str:
        return "ctrl-1"

    async def _enter(self) -> None:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)

    async def read_telemetry(self, reactor_id: str) -> object:
        await self._enter()
        try:
            while not self.released.is_set():
                try:
                    await self.released.wait()
                except asyncio.CancelledError:
                    continue  # ignores cancellation, so the lock stays held
        finally:
            self.in_flight -= 1
        return None

    async def short_read(self) -> None:
        await self._enter()
        await asyncio.sleep(0.01)
        self.in_flight -= 1

    async def enter_safe_state(self, reactor_id: str, reason: str) -> SafeStateAcknowledgement:
        await self._enter()
        self.in_flight -= 1
        self.safed.append(reactor_id)
        return SafeStateAcknowledgement(
            controller_id="ctrl-1",
            reactor_id=reactor_id,
            reason=reason,
            reached=True,
            acknowledged_at=START,
        )


def test_a_hung_call_holding_the_batch_lock_cannot_block_safe_state() -> None:
    async def main() -> list[str]:
        probe = _LockProbe(released=asyncio.Event())
        serial = SerialController(probe, safe_state_lock_wait_seconds=0.05)  # type: ignore[arg-type]
        stuck = asyncio.ensure_future(serial.read_telemetry("reactor-1"))
        await asyncio.sleep(0)
        await asyncio.wait_for(serial.enter_safe_state("reactor-2", "round ended"), 2)
        await asyncio.wait_for(serial.enter_safe_state("reactor-3", "round ended"), 2)
        probe.released.set()
        await stuck
        return probe.safed

    assert asyncio.run(main()) == ["reactor-2", "reactor-3"]


def test_safe_state_still_queues_behind_a_short_call() -> None:
    async def main() -> int:
        probe = _LockProbe(released=asyncio.Event())
        serial = SerialController(probe, safe_state_lock_wait_seconds=1)  # type: ignore[arg-type]

        async def locked_short_read() -> None:
            async with serial._lock:  # pyright: ignore[reportPrivateUsage]
                await probe.short_read()

        await asyncio.gather(
            locked_short_read(), serial.enter_safe_state("reactor-1", "x"), locked_short_read()
        )
        return probe.max_in_flight

    assert asyncio.run(main()) == 1
