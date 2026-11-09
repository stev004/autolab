"""Time for long-running experiments, injected so the runner never hard-codes waiting.

A real 36-hour culture must actually wait 36 hours; a simulated one must not.
The runner only ever asks the clock "what time is it?" and "wake me at t", so
the same runner drives both. ``SteppedClock`` jumps straight to each wake-up time
and notifies listeners, which is how a simulator advances its world in lockstep.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from autonomous_lab.domain.identifiers import require_utc


class CultureClock(Protocol):
    def now(self) -> datetime: ...

    async def sleep_until(self, when: datetime) -> None: ...


class WallClock:
    """Real time. Suitable for hardware; a culture run will take its full duration."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep_until(self, when: datetime) -> None:
        delay = (require_utc(when) - self.now()).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)


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
        require_utc(when)
        if when > self._now:
            self._now = when
            for listener in self._listeners:
                listener(when)
        await asyncio.sleep(0)
