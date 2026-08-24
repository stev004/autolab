"""Centrally enforced campaign and experiment lifecycle transitions."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, field_validator

from autonomous_lab.domain.base import DomainModel
from autonomous_lab.domain.enums import CampaignState, ExperimentState
from autonomous_lab.domain.identifiers import require_utc, utc_now


class InvalidStateTransition(ValueError):
    """Raised when a lifecycle transition is not explicitly permitted."""


class StateTransition(DomainModel):
    """Audit-ready state transition emitted by every successful transition."""

    schema_version: Literal["1"] = "1"
    entity_type: Literal["campaign", "experiment"]
    entity_id: UUID
    from_state: str
    to_state: str
    occurred_at: datetime = Field(default_factory=utc_now)
    reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def _utc_occurred_at(cls, value: datetime) -> datetime:
        return require_utc(value)


CAMPAIGN_TRANSITIONS: dict[CampaignState, frozenset[CampaignState]] = {
    CampaignState.DRAFT: frozenset({CampaignState.READY}),
    CampaignState.READY: frozenset({CampaignState.RUNNING}),
    CampaignState.RUNNING: frozenset(
        {CampaignState.PAUSED, CampaignState.COMPLETED, CampaignState.FAILED}
    ),
    CampaignState.PAUSED: frozenset({CampaignState.RUNNING, CampaignState.FAILED}),
    CampaignState.COMPLETED: frozenset(),
    CampaignState.FAILED: frozenset(),
}

EXPERIMENT_TRANSITIONS: dict[ExperimentState, frozenset[ExperimentState]] = {
    ExperimentState.PROPOSED: frozenset(
        {ExperimentState.VALIDATED, ExperimentState.REJECTED}
    ),
    ExperimentState.VALIDATED: frozenset({ExperimentState.QUEUED}),
    ExperimentState.REJECTED: frozenset(),
    ExperimentState.QUEUED: frozenset(
        {
            ExperimentState.AWAITING_EXECUTION,
            ExperimentState.RUNNING,
            ExperimentState.FAILED,
            ExperimentState.ABORTED,
        }
    ),
    ExperimentState.AWAITING_EXECUTION: frozenset(
        {ExperimentState.RUNNING, ExperimentState.ABORTED, ExperimentState.FAILED}
    ),
    ExperimentState.RUNNING: frozenset(
        {
            ExperimentState.AWAITING_MEASUREMENTS,
            ExperimentState.MEASURED,
            ExperimentState.FAILED,
            ExperimentState.ABORTED,
        }
    ),
    ExperimentState.AWAITING_MEASUREMENTS: frozenset(
        {ExperimentState.MEASURED, ExperimentState.FAILED, ExperimentState.ABORTED}
    ),
    ExperimentState.MEASURED: frozenset(
        {ExperimentState.QC_PASSED, ExperimentState.QC_FAILED}
    ),
    ExperimentState.FAILED: frozenset(),
    ExperimentState.ABORTED: frozenset(),
    ExperimentState.QC_PASSED: frozenset({ExperimentState.ANALYSED}),
    ExperimentState.QC_FAILED: frozenset(),
    ExperimentState.ANALYSED: frozenset({ExperimentState.OBSERVED}),
    ExperimentState.OBSERVED: frozenset(),
}


def transition_campaign(
    entity_id: UUID,
    current: CampaignState,
    target: CampaignState,
    *,
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> StateTransition:
    """Validate a campaign transition and return its audit event."""

    _require_allowed(current, target, CAMPAIGN_TRANSITIONS, "campaign")
    return StateTransition(
        entity_type="campaign",
        entity_id=entity_id,
        from_state=current.value,
        to_state=target.value,
        reason=reason,
        metadata=metadata or {},
    )


def transition_experiment(
    entity_id: UUID,
    current: ExperimentState,
    target: ExperimentState,
    *,
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> StateTransition:
    """Validate an experiment transition and return its audit event."""

    _require_allowed(current, target, EXPERIMENT_TRANSITIONS, "experiment")
    return StateTransition(
        entity_type="experiment",
        entity_id=entity_id,
        from_state=current.value,
        to_state=target.value,
        reason=reason,
        metadata=metadata or {},
    )


def _require_allowed(
    current: CampaignState | ExperimentState,
    target: CampaignState | ExperimentState,
    transitions: dict[Any, frozenset[Any]],
    entity_type: str,
) -> None:
    if target not in transitions[current]:
        raise InvalidStateTransition(
            f"illegal {entity_type} state transition: {current.value} -> {target.value}"
        )
