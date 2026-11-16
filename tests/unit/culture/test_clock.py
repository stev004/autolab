"""Clocks: WallClock measures elapsed time monotonically; SteppedClock never rewinds."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from autonomous_lab.culture import SteppedClock, WallClock

ANCHOR = datetime(2026, 11, 1, 8, tzinfo=UTC)


class _FakeTime:
    """A system clock that can be stepped (NTP, an operator) and a monotonic one."""

    def __init__(self) -> None:
        self.wall = ANCHOR
        self.monotonic = 1000.0
        self.sleeps: list[float] = []

    def advance(self, seconds: float) -> None:
        self.wall += timedelta(seconds=seconds)
        self.monotonic += seconds

    async def sleep(self, seconds: float) -> None:
        # Wake slightly early, as real timers may: the clock must sleep again.
        self.sleeps.append(seconds)
        self.advance(max(seconds - 0.25, 0.1))


def _clock(fake: _FakeTime) -> WallClock:
    return WallClock(wall=lambda: fake.wall, monotonic=lambda: fake.monotonic, sleep=fake.sleep)


def test_now_starts_at_the_system_time_and_follows_elapsed_time() -> None:
    fake = _FakeTime()
    clock = _clock(fake)
    assert clock.now() == ANCHOR
    fake.advance(90)
    assert clock.now() == ANCHOR + timedelta(seconds=90)
    assert clock.now().tzinfo is UTC


def test_a_system_clock_step_does_not_move_now() -> None:
    fake = _FakeTime()
    clock = _clock(fake)
    fake.advance(10)
    fake.wall -= timedelta(hours=1)  # NTP or an operator sets the clock back
    assert clock.now() == ANCHOR + timedelta(seconds=10)
    fake.wall += timedelta(days=1)  # ...or forward
    assert clock.now() == ANCHOR + timedelta(seconds=10)


def test_sleep_until_does_not_return_early() -> None:
    fake = _FakeTime()
    clock = _clock(fake)
    target = ANCHOR + timedelta(seconds=5)
    asyncio.run(clock.sleep_until(target))
    assert clock.now() >= target
    assert len(fake.sleeps) > 1  # it slept again after the early wake-up


def test_sleep_until_a_past_time_returns_at_once() -> None:
    fake = _FakeTime()
    clock = _clock(fake)
    asyncio.run(clock.sleep_until(ANCHOR - timedelta(minutes=1)))
    assert fake.sleeps == []


def test_naive_times_are_refused() -> None:
    fake = _FakeTime()
    with pytest.raises(ValueError):
        asyncio.run(_clock(fake).sleep_until(datetime(2026, 11, 1, 9)))


def test_the_default_wall_clock_really_waits() -> None:
    clock = WallClock()
    start = clock.now()
    asyncio.run(clock.sleep_until(start + timedelta(milliseconds=30)))
    assert clock.now() - start >= timedelta(milliseconds=30)
    assert abs(clock.now() - datetime.now(UTC)) < timedelta(seconds=1)


def test_stepped_clock_never_goes_backwards_and_notifies_listeners() -> None:
    clock = SteppedClock(ANCHOR)
    seen: list[datetime] = []
    clock.subscribe(seen.append)
    asyncio.run(clock.sleep_until(ANCHOR + timedelta(hours=1)))
    asyncio.run(clock.sleep_until(ANCHOR))  # in the past: no change
    assert clock.now() == ANCHOR + timedelta(hours=1)
    assert seen == [ANCHOR + timedelta(hours=1)]
