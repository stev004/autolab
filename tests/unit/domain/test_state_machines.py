from uuid import uuid4

import pytest

from autonomous_lab.domain.enums import CampaignState, ExperimentState
from autonomous_lab.domain.state_machines import (
    InvalidStateTransition,
    transition_campaign,
    transition_experiment,
)


def test_campaign_transition_emits_audit_ready_event() -> None:
    campaign_id = uuid4()

    event = transition_campaign(campaign_id, CampaignState.DRAFT, CampaignState.READY)

    assert event.entity_id == campaign_id
    assert event.entity_type == "campaign"
    assert event.from_state == "draft"
    assert event.to_state == "ready"
    assert event.occurred_at.utcoffset().total_seconds() == 0


def test_illegal_campaign_transition_fails_loudly() -> None:
    with pytest.raises(InvalidStateTransition, match="draft -> completed"):
        transition_campaign(uuid4(), CampaignState.DRAFT, CampaignState.COMPLETED)


def test_qc_failure_is_terminal_and_cannot_be_observed() -> None:
    with pytest.raises(InvalidStateTransition, match="qc_failed -> observed"):
        transition_experiment(
            uuid4(), ExperimentState.QC_FAILED, ExperimentState.OBSERVED
        )


def test_scientifically_valid_analysis_can_reach_observed() -> None:
    event = transition_experiment(
        uuid4(), ExperimentState.ANALYSED, ExperimentState.OBSERVED
    )
    assert event.to_state == "observed"

