"""Time for long-running experiments, injected so the runner never hard-codes waiting.

A real 36-hour culture must actually wait 36 hours; a simulated one must not.
The runner only ever asks the clock "what time is it?" and "wake me at t", so
the same runner drives both. ``SteppedClock`` jumps straight to each wake-up time
and notifies listeners, which is how a simulator advances its world in lockstep.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from autonomous_lab.domain.identifiers import require_utc


class CultureClock(Protocol):
    def now(self) -> datetime: ...

    async def sleep_until(self, when: datetime) -> None: ...


def _system_utc() -> datetime:
    return datetime.now(UTC)


class WallClock:
    """Real time. Suitable for hardware; a culture run will take its full duration.

    ``now()`` reads the system's UTC time once, when the clock is created, and
    from then on adds elapsed time measured by ``time.monotonic()``. The system
    clock can be stepped while a culture runs (NTP correcting it, an operator, a
    VM resuming), and a step would move a sample schedule computed from it, or
    even make time run backwards. A monotonic source cannot be stepped.

    The cost: over a long run ``now()`` can drift from the system clock by the
    monotonic clock's rate error. That is at worst a few seconds a day for an
    undisciplined crystal (Linux slews its monotonic clock with NTP, so far
    less there), well inside minutes-wide sample tolerance windows. Device and
    sensor timestamps still come from their own clocks.
    """

    def __init__(
        self,
        *,
        wall: Callable[[], datetime] = _system_utc,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        # Injectable so tests can step the system clock without waiting.
        self._anchor = require_utc(wall())
        self._started = monotonic()
        self._monotonic = monotonic
        self._sleep = sleep

    def now(self) -> datetime:
        return self._anchor + timedelta(seconds=self._monotonic() - self._started)

    async def sleep_until(self, when: datetime) -> None:
        require_utc(when)
        # Timers may fire a little early; never return before ``when``.
        while (delay := (when - self.now()).total_seconds()) > 0:
            await self._sleep(delay)


class SteppedClock:
    """Simulated time for tests and simulators. Never goes backwards."""

    def __init__(self, start: datetime) -> None:
        self._now = require_utc(start)
        self._listeners: list[Callable[[datetime], None]] = []

    def subscribe(self, listener: Callable[[datetime], None]) -> None:
        """Called with the new time after every advance (e.g. to step a simulator)."""

        self._listeners.append(listener)

    def now(self) -> datetime:
        return self._now

    async def sleep_until(self, when: datetime) -> None:
        self.advance_to(when)
        await asyncio.sleep(0)

    def advance_to(self, when: datetime) -> None:
        """Jump forward to ``when`` (never backwards) and notify listeners."""

        require_utc(when)
        if when > self._now:
            self._now = when
            for listener in self._listeners:
                listener(when)
