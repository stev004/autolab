"""Capability-limited interface for a future LLM or research agent.

PLACEHOLDER: this module only defines types and a protocol. No agent implementation
exists yet, and nothing in the application calls it.

The objects in this module are scientific documents, never device commands.  An
agent implementation can propose a campaign or interpret a completed summary,
but the application service must still validate the draft and owns every later
compiler, validator, orchestrator, and hardware interaction.
"""

from __future__ import annotations

from typing import Literal, Protocol
from uuid import UUID

from autonomous_lab.domain.base import DomainModel
from autonomous_lab.domain.objectives import ScientificObjective
from autonomous_lab.domain.parameters import ParameterSpace


class CampaignDraft(DomainModel):
    """Untrusted structured scientific intent produced outside the lab runtime."""

    schema_version: Literal["1"] = "1"
    name: str
    hypothesis: str
    objective: ScientificObjective
    parameter_space: ParameterSpace
    rationale: str | None = None


class CampaignOutcomeSummary(DomainModel):
    """Read-only scientific evidence summary safe to expose to an agent."""

    schema_version: Literal["1"] = "1"
    campaign_id: UUID
    experiment_count: int
    qc_valid_count: int
    feasible_count: int
    pareto_frontier: tuple[dict[str, float | str], ...]
    limitations: tuple[str, ...] = ()


class ResearchAgent(Protocol):
    """Agent-side contract with deliberately no execution capability."""

    def propose_campaign(self, objective: ScientificObjective) -> CampaignDraft: ...

    def interpret_campaign(self, summary: CampaignOutcomeSummary) -> str: ...
