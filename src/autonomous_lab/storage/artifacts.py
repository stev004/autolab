"""Content-addressed storage for raw binary artifacts."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse


@dataclass(frozen=True)
class StoredArtifact:
    uri: str
    sha256: str
    byte_count: int
    media_type: str


class ArtifactStore:
    """Write immutable files under a SHA-256-derived path.

    Database metadata is intentionally registered separately through
    :class:`ProvenanceRepository`, allowing the file write and database
    transaction to have explicit failure handling at the application layer.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put_bytes(
        self,
        content: bytes,
        *,
        media_type: str = "application/octet-stream",
        suffix: str = "",
    ) -> StoredArtifact:
        digest = hashlib.sha256(content).hexdigest()
        clean_suffix = _safe_suffix(suffix)
        target = self.root / digest[:2] / f"{digest}{clean_suffix}"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            descriptor, temporary_name = tempfile.mkstemp(prefix=f".{digest}.", dir=target.parent)
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        return StoredArtifact(
            uri=target.as_uri(),
            sha256=digest,
            byte_count=len(content),
            media_type=media_type,
        )

    def put_file(
        self,
        source: str | Path,
        *,
        media_type: str = "application/octet-stream",
    ) -> StoredArtifact:
        path = Path(source)
        return self.put_bytes(path.read_bytes(), media_type=media_type, suffix=path.suffix)

    def resolve(self, artifact: StoredArtifact) -> Path:
        parsed = urlparse(artifact.uri)
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            raise ValueError("Artifact URI is not a local file URI")
        path = Path(unquote(parsed.path)).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Artifact URI is outside this store")
        return path


def _safe_suffix(value: str) -> str:
    if not value:
        return ""
    suffix = value if value.startswith(".") else f".{value}"
    allowed = ".-_abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    if Path(suffix).name != suffix or any(char not in allowed for char in suffix):
        raise ValueError(f"Unsafe artifact suffix: {value!r}")
    return suffix
