"""Durable SQLite persistence and provenance for AutoLab."""

from autonomous_lab.storage.artifacts import ArtifactStore, StoredArtifact
from autonomous_lab.storage.database import Database, sqlite_pragmas
from autonomous_lab.storage.orm import Base, ImmutableRecordError
from autonomous_lab.storage.repositories import (
    CampaignSummary,
    RecordNotFoundError,
    StateConflictError,
)
from autonomous_lab.storage.unit_of_work import Storage, UnitOfWork

__all__ = [
    "ArtifactStore",
    "Base",
    "CampaignSummary",
    "Database",
    "ImmutableRecordError",
    "RecordNotFoundError",
    "StateConflictError",
    "Storage",
    "StoredArtifact",
    "UnitOfWork",
    "sqlite_pragmas",
]
