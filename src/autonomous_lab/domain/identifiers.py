"""Identifier and timestamp helpers."""

from datetime import UTC, datetime
from typing import NewType
from uuid import UUID, uuid4

CampaignId = NewType("CampaignId", UUID)
ExperimentId = NewType("ExperimentId", UUID)
ProposalId = NewType("ProposalId", UUID)
ProtocolId = NewType("ProtocolId", UUID)
MeasurementId = NewType("MeasurementId", UUID)
RunId = NewType("RunId", UUID)


def new_uuid() -> UUID:
    """Return a random UUID suitable for persisted entity identity."""

    return uuid4()


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""

    return datetime.now(UTC)


def require_utc(value: datetime) -> datetime:
    """Reject naive or non-UTC timestamps to keep provenance unambiguous."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    if value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("timestamp must use UTC")
    return value
