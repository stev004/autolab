"""Sharing one rig between concurrent culture runs (parallel reactor batches).

A batch round runs several experiments at once, each on its own reactor group, all
driven through one controller, one sensor and one clock. Adapters are not required
to tolerate concurrent calls, so AutoLab serialises them here:

- ``SerialController`` puts a lock around every controller call, except that a
  safe-state request stops waiting for the lock if another call has held it too
  long (a hung call must not keep every group from reaching safe state).
- ``SharedSensor`` keeps one sensor session open for the whole round. A run
  finishing early must not close the probe under the runs still sampling.
- ``LockstepClock`` lets simulated time advance only once every live run is
  waiting, so no run's sample time is skipped by another run's jump.

None of this is used when a single experiment runs alone; that path is unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import itertools
from datetime import datetime

from autonomous_lab.culture.clock import CultureClock, SteppedClock
from autonomous_lab.culture.controller import (
    CultureEnvironmentController,
    EnvironmentTelemetry,
    SafeStateAcknowledgement,
    SetpointAcknowledgement,
    SetpointCommand,
)
from autonomous_lab.culture.models import CultureControllerCapabilities
from autonomous_lab.domain.identifiers import require_utc
from autonomous_lab.hardware.sensors.contracts import (
    MeasurementRequest,
    RawSensorMeasurement,
    ReadOnlySensor,
    SelfTestResult,
    SensorCapabilities,
    SensorConnection,
)
from autonomous_lab.hardware.sensors.errors import SensorError


class SerialController:
    """One call at a time to the wrapped controller.

    Safe state outranks serialisation. If a call hangs while holding the lock
    (the runner abandons it after its timeout, but an adapter that ignores
    cancellation keeps the lock), every later call would queue behind it, so
    every group's safe-state sweep would time out without reaching the device.
    So ``enter_safe_state`` waits at most ``safe_state_lock_wait_seconds`` for the
    lock and then calls the adapter anyway. By then one-call-at-a-time is already
    broken by the hung call; a concurrent safe-state request is the lesser risk.
    The wait must be shorter than the runner's ``safe_state_timeout_seconds`` or
    the runner gives up first.
    """

    def __init__(
        self, inner: CultureEnvironmentController, *, safe_state_lock_wait_seconds: float = 5.0
    ) -> None:
        if safe_state_lock_wait_seconds <= 0:
            raise ValueError("safe_state_lock_wait_seconds must be positive")
        self.inner = inner
        self._lock = asyncio.Lock()
        self.safe_state_lock_wait_seconds = safe_state_lock_wait_seconds

    @property
    def controller_id(self) -> str:
        return self.inner.controller_id

    async def capability_manifest(self) -> CultureControllerCapabilities:
        async with self._lock:
            return await self.inner.capability_manifest()

    async def apply_setpoints(self, command: SetpointCommand) -> SetpointAcknowledgement:
        async with self._lock:
            return await self.inner.apply_setpoints(command)

    async def read_telemetry(self, reactor_id: str) -> EnvironmentTelemetry:
        async with self._lock:
            return await self.inner.read_telemetry(reactor_id)

    async def enter_safe_state(self, reactor_id: str, reason: str) -> SafeStateAcknowledgement:
        try:
            await asyncio.wait_for(self._lock.acquire(), self.safe_state_lock_wait_seconds)
        except TimeoutError:
            # The lock holder is stuck: go around it (see the class docstring).
            return await self.inner.enter_safe_state(reactor_id, reason)
        try:
            return await self.inner.enter_safe_state(reactor_id, reason)
        finally:
            self._lock.release()


class SharedSensor:
    """One sensor session for a whole round; calls are serialised.

    This is round-scoped, not reference-counted. ``open`` opens the real sensor
    only if no session is open, and otherwise returns the existing session.
    ``close`` from a run is a no-op while that session is open; the round owner
    calls :meth:`close_session` once, after every run has ended. If no session
    was ever opened through this wrapper, ``close`` passes through to the sensor.
    """

    def __init__(self, inner: ReadOnlySensor) -> None:
        self.inner = inner
        self._lock = asyncio.Lock()
        self._connection: SensorConnection | None = None

    @property
    def sensor_id(self) -> str:
        return self.inner.sensor_id

    @property
    def is_open(self) -> bool:
        return self.inner.is_open

    async def open(self) -> SensorConnection:
        async with self._lock:
            if not self.inner.is_open or self._connection is None:
                self._connection = await self.inner.open()
            return self._connection

    async def close(self) -> SensorConnection:
        if self._connection is None:
            async with self._lock:
                return await self.inner.close()
        return self._connection

    async def close_session(self) -> None:
        async with self._lock:
            if self.inner.is_open:
                # A read-only sensor that fails to close cannot harm the culture.
                with contextlib.suppress(SensorError):
                    await self.inner.close()
            self._connection = None

    async def capability_manifest(self) -> SensorCapabilities:
        async with self._lock:
            return await self.inner.capability_manifest()

    async def self_test(self) -> SelfTestResult:
        async with self._lock:
            return await self.inner.self_test()

    async def read(self, request: MeasurementRequest) -> RawSensorMeasurement:
        async with self._lock:
            return await self.inner.read(request)


class LockstepClock:
    """A stepped clock shared by concurrent runs.

    Each run joins before it starts and leaves when it ends. Time advances to the
    earliest requested wake-up only when every joined run is waiting, then wakes
    every run due at that time. Listeners on the wrapped clock (a simulator) see
    the same advances they would see from one run.
    """

    def __init__(self, inner: SteppedClock) -> None:
        self.inner = inner
        self._active = 0
        self._waiting: list[tuple[datetime, int, asyncio.Future[None]]] = []
        self._order = itertools.count()

    def now(self) -> datetime:
        return self.inner.now()

    def join(self) -> None:
        self._active += 1

    def leave(self) -> None:
        self._active -= 1
        self._advance_if_all_waiting()

    async def sleep_until(self, when: datetime) -> None:
        require_utc(when)
        if when > self.inner.now():
            future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            heapq.heappush(self._waiting, (when, next(self._order), future))
            self._advance_if_all_waiting()
            await future
        await asyncio.sleep(0)

    def _advance_if_all_waiting(self) -> None:
        # A cancelled sleeper's entry is done; it no longer counts as waiting.
        while self._waiting and self._waiting[0][2].done():
            heapq.heappop(self._waiting)
        sleeping = sum(1 for _, _, future in self._waiting if not future.done())
        if not sleeping or sleeping < self._active:
            return
        target = self._waiting[0][0]
        self.inner.advance_to(target)
        while self._waiting and self._waiting[0][0] <= target:
            _, _, future = heapq.heappop(self._waiting)
            if not future.done():
                future.set_result(None)
        # Woken runs are busy again; the next advance waits until they sleep or leave.


def shared_clock(clock: CultureClock) -> CultureClock:
    """The clock concurrent runs share: lockstep for simulated time, else as is."""

    if isinstance(clock, SteppedClock):
        return LockstepClock(clock)
    return clock
