"""The per-campaign writer lock: exclusive, never waits, released when the holder dies."""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path
from uuid import uuid4

import pytest

from autonomous_lab.campaigns.writer_lock import CampaignLockedError, campaign_writer_lock
from autonomous_lab.storage import Database, Storage

CAMPAIGN = uuid4()


def _storage(tmp_path: Path) -> Storage:
    return Storage(Database(tmp_path / "autolab.sqlite3"))


def test_a_second_writer_is_refused_and_the_lock_is_reusable_after_release(
    tmp_path: Path,
) -> None:
    first, second = _storage(tmp_path), _storage(tmp_path)
    with campaign_writer_lock(first, CAMPAIGN):
        with (
            pytest.raises(CampaignLockedError, match=r"pid \d+"),
            campaign_writer_lock(second, CAMPAIGN),
        ):
            pass
        # Other campaigns in the same database are independent.
        with campaign_writer_lock(second, uuid4()):
            pass
    with campaign_writer_lock(second, CAMPAIGN):
        pass


def test_the_lock_is_released_when_its_holder_process_is_killed(tmp_path: Path) -> None:
    holder = textwrap.dedent(
        f"""
        import sys, time
        from uuid import UUID
        from autonomous_lab.campaigns.writer_lock import campaign_writer_lock
        from autonomous_lab.storage import Database, Storage
        storage = Storage(Database({str(tmp_path / "autolab.sqlite3")!r}))
        with campaign_writer_lock(storage, UUID({str(CAMPAIGN)!r})):
            print("locked", flush=True)
            time.sleep(60)
        """
    )
    process = subprocess.Popen([sys.executable, "-c", holder], stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "locked"
        with (
            pytest.raises(CampaignLockedError),
            campaign_writer_lock(_storage(tmp_path), CAMPAIGN),
        ):
            pass
    finally:
        process.kill()  # a crash: no cleanup code runs in the holder
        process.wait()
    deadline = time.monotonic() + 5
    while True:
        try:
            with campaign_writer_lock(_storage(tmp_path), CAMPAIGN):
                break
        except CampaignLockedError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def test_in_memory_databases_lock_within_the_process() -> None:
    storage = Storage(Database("sqlite://"))
    with (
        campaign_writer_lock(storage, CAMPAIGN),
        pytest.raises(CampaignLockedError, match="in this process"),
        campaign_writer_lock(storage, CAMPAIGN),
    ):
        pass
    with campaign_writer_lock(storage, CAMPAIGN):
        pass
