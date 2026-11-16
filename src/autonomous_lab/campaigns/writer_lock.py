"""One writer per campaign: a second ``culture run`` must refuse, not "resume".

Recovery treats every RUNNING experiment as interrupted, because after a crash
that is exactly what it is. But if the first process is still alive, the second
one would "resume" the live run and drive the same reactors twice. So whoever
runs a campaign holds an exclusive lock on it for the whole run.

The lock is an OS advisory file lock (``flock`` on POSIX, ``msvcrt.locking`` on
Windows) on ``<database>.locks/<campaign_id>.lock``, next to the SQLite file.
That choice matters for crash safety: the operating system releases the lock
when the holder's process dies, however it dies. A lease row in the database
would need heartbeats and an expiry guess, and a stale lease after a crash
would block the very recovery it exists to protect. The cost is scope: an OS
lock only excludes processes on the same host, which is also where the SQLite
file lives. A shared database server would need a database-level lease instead.

The lock file is left in place after release. Deleting it would race with a
process that has just opened it, and an unlocked file means nothing.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import IO
from uuid import UUID

from autonomous_lab.campaigns.lifecycle import CampaignExecutionError
from autonomous_lab.domain.identifiers import utc_now
from autonomous_lab.storage import Storage


class CampaignLockedError(CampaignExecutionError):
    """Another live process is already running this campaign."""


# In-memory databases cannot be shared between processes, so for them a
# process-local registry is the whole story.
_memory_locks: set[tuple[int, str]] = set()
_memory_guard = threading.Lock()


@contextmanager
def campaign_writer_lock(storage: Storage, campaign_id: UUID) -> Generator[None]:
    """Hold the exclusive writer lock for ``campaign_id``, or refuse at once.

    Never waits: a campaign that is already running is an operator mistake to
    report, not a queue to join.
    """

    database = storage.database.engine.url.database
    if not database or database == ":memory:":
        with _memory_lock(id(storage.database.engine), str(campaign_id)):
            yield
        return
    path = Path(database).with_name(Path(database).name + ".locks") / f"{campaign_id}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    # "a+" creates the file without truncating the current holder's note.
    with path.open("a+", encoding="utf-8") as handle:
        if not _try_lock(handle):
            handle.seek(0)
            holder = handle.read().strip() or "unknown process"
            raise CampaignLockedError(
                f"campaign {campaign_id} is already being run by another process "
                f"({holder}); refusing to start a second writer. If no AutoLab "
                "process is running it, the lock is already free: try again."
            )
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(f"pid {os.getpid()} on {socket.gethostname()} since {utc_now()}\n")
            handle.flush()
            yield
        finally:
            _unlock(handle)


@contextmanager
def _memory_lock(engine_id: int, campaign_id: str) -> Generator[None]:
    key = (engine_id, campaign_id)
    with _memory_guard:
        if key in _memory_locks:
            raise CampaignLockedError(
                f"campaign {campaign_id} is already being run in this process"
            )
        _memory_locks.add(key)
    try:
        yield
    finally:
        with _memory_guard:
            _memory_locks.discard(key)


if sys.platform == "win32":
    import msvcrt

    def _try_lock(handle: IO[str]) -> bool:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(handle: IO[str]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(handle: IO[str]) -> bool:
        # flock (not lockf): it locks the open file description, so even two
        # opens within one process exclude each other.
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def _unlock(handle: IO[str]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
