"""Immutable capability snapshots used for validation receipts."""

from typing import Literal

from autonomous_lab.domain.base import DomainModel
from autonomous_lab.domain.protocols import CapabilityManifest
from autonomous_lab.provenance.hashing import canonical_sha256


class CapabilitySnapshot(DomainModel):
    schema_version: Literal["1"] = "1"
    manifests: tuple[CapabilityManifest, ...]

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)
