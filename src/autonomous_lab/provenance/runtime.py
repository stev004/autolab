"""Capture the software environment attached to scientific artifacts."""

from __future__ import annotations

import hashlib
import os
import platform
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field, field_validator

from autonomous_lab.domain.base import DomainModel
from autonomous_lab.domain.identifiers import require_utc, utc_now
from autonomous_lab.provenance.hashing import sha256_file
from autonomous_lab.version import __version__


class RuntimeMetadata(DomainModel):
    schema_version: Literal["1"] = "1"
    application_version: str
    python_version: str
    implementation: str
    operating_system: str
    platform: str
    machine: str
    git_commit: str | None
    git_dirty: bool | None = None
    source_tree_diff_sha256: str | None = None
    uv_lock_sha256: str | None
    captured_at: datetime = Field(default_factory=utc_now)

    @field_validator("captured_at")
    @classmethod
    def _utc_captured_at(cls, value: datetime) -> datetime:
        return require_utc(value)


def capture_runtime_metadata(project_root: str | Path | None = None) -> RuntimeMetadata:
    root = Path(project_root).resolve() if project_root is not None else _discover_project_root()
    lock_path = root / "uv.lock"
    git_commit = _git_commit(root)
    git_dirty, source_tree_diff_sha256 = _git_source_state(root)
    return RuntimeMetadata(
        application_version=__version__,
        python_version=platform.python_version(),
        implementation=platform.python_implementation(),
        operating_system=platform.system(),
        platform=platform.platform(),
        machine=platform.machine(),
        git_commit=git_commit,
        git_dirty=git_dirty,
        source_tree_diff_sha256=source_tree_diff_sha256,
        uv_lock_sha256=sha256_file(lock_path) if lock_path.is_file() else None,
    )


def _discover_project_root() -> Path:
    for candidate in (Path.cwd(), *Path.cwd().parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return Path.cwd()


def _git_commit(root: Path) -> str | None:
    output = _git_output(root, "rev-parse", "HEAD")
    if output is None:
        return None
    commit = output.decode(errors="replace").strip()
    return commit or None


def _git_source_state(root: Path) -> tuple[bool | None, str | None]:
    """Return dirty state and a content-sensitive hash without exposing a diff."""

    status = _git_output(root, "status", "--porcelain=v1", "--untracked-files=all")
    if status is None:
        return None, None
    if not status:
        return False, None

    digest = hashlib.sha256()
    _update_digest(digest, b"status", status)
    for label, arguments in (
        (b"staged-diff", ("diff", "--cached", "--binary", "--no-ext-diff", "--")),
        (b"working-diff", ("diff", "--binary", "--no-ext-diff", "--")),
    ):
        diff = _git_output(root, *arguments)
        if diff is None:
            return True, None
        _update_digest(digest, label, diff)

    untracked = _git_output(root, "ls-files", "--others", "--exclude-standard", "-z")
    if untracked is None:
        return True, None
    for raw_path in sorted(item for item in untracked.split(b"\0") if item):
        _update_digest(digest, b"untracked-path", raw_path)
        relative = Path(os.fsdecode(raw_path))
        if relative.is_absolute() or ".." in relative.parts:
            _update_digest(digest, b"untracked-invalid", b"")
            continue
        path = root / relative
        if path.is_symlink():
            _update_digest(digest, b"untracked-symlink", os.fsencode(os.readlink(path)))
        elif path.is_file():
            _update_file_digest(digest, path)
        else:
            _update_digest(digest, b"untracked-non-file", b"")
    return True, digest.hexdigest()


def _git_output(root: Path, *arguments: str) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=True,
            capture_output=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout


def _update_digest(digest: AnyHash, label: bytes, value: bytes) -> None:
    digest.update(len(label).to_bytes(4, "big"))
    digest.update(label)
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _update_file_digest(digest: AnyHash, path: Path) -> None:
    digest.update(b"untracked-file")
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)


class AnyHash(Protocol):
    """Structural subset shared by hashlib hash implementations."""

    def update(self, data: bytes, /) -> None: ...
