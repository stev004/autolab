"""Capability-limited boundary for research agents (roadmap stage 7).

An agent here can do exactly two things:

1. **Draft** a campaign document from a research goal. The draft is untrusted:
   the application reviews it with the same strict config validation and trusted
   installation policy as any hand-written campaign, and only a person turns an
   accepted draft into a campaign.
2. **Interpret** a read-only evidence digest of a campaign.

Every object in this module is a plain scientific document. None carries a
device, an orchestrator, a validator, an optimiser or storage handle, so an
agent implementation has nothing it could use to run an experiment. An
architecture test enforces that this package never imports the execution layers.

Implementations: ``rule_based.TemplateResearchAgent`` (deterministic, no model,
no cost). An external agent such as Claude Code can play the same role through
the ``autolab agent`` CLI (see ``docs/agents.md``). A hosted or local model can be
added later behind the same protocol.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import Field, field_validator

from autonomous_lab.domain.base import DomainModel
from autonomous_lab.domain.enums import ObjectiveDirection

ExperimentType = Literal["assay", "culture"]


class ObjectiveHint(DomainModel):
    """What the person wants optimised, in metric-registry terms."""

    metric: str
    direction: ObjectiveDirection


class ResearchGoal(DomainModel):
    """A person's research intent. Untrusted free text plus a few structured hints."""

    schema_version: Literal["1"] = "1"
    title: str = Field(min_length=1, max_length=120)
    statement: str = Field(min_length=1, max_length=4000)
    experiment_type: ExperimentType
    objectives: tuple[ObjectiveHint, ...] = ()
    max_experiments: int | None = Field(default=None, ge=1, le=500)
    #: Optional narrower search ranges, {parameter: [minimum, maximum]} in the
    #: template's units. Drafts may only narrow, never widen, the template.
    parameter_focus: dict[str, tuple[float, float]] = Field(default_factory=dict)


class DraftingContext(DomainModel):
    """Read-only facts the application gives an agent to draft against.

    ``template`` is a known-good campaign document chosen by the operator; agents
    adapt it rather than inventing device or execution settings.
    """

    template: dict[str, Any]
    available_metrics: tuple[str, ...]
    policy_limits: dict[str, Any] = Field(default_factory=dict)


class CampaignDraft(DomainModel):
    """Untrusted campaign proposal produced outside the lab runtime."""

    schema_version: Literal["2"] = "2"
    author: str = Field(min_length=1, description="agent name and version, or a person")
    goal: ResearchGoal
    hypothesis: str = Field(min_length=1, max_length=4000)
    rationale: str = Field(min_length=1, max_length=8000)
    assumptions: tuple[str, ...] = ()
    #: The proposed campaign configuration, exactly as it would appear in YAML.
    document: dict[str, Any]


class ExperimentDigest(DomainModel):
    ordinal: int
    experiment_id: UUID
    state: str
    parameters: dict[str, Any]
    metrics: dict[str, float] = Field(default_factory=dict)
    failed_checks: tuple[str, ...] = ()
    failure_reason: str | None = None
    contamination_flagged: bool = False


class EvidenceDigest(DomainModel):
    """Read-only scientific summary of one campaign, safe to give an agent.

    It contains values and statuses, never raw device handles, file paths or
    credentials.
    """

    schema_version: Literal["1"] = "1"
    campaign_id: UUID
    campaign_name: str
    experiment_type: ExperimentType
    state: str
    objectives: tuple[ObjectiveHint, ...]
    parameter_ranges: dict[str, tuple[float, float]]
    experiments: tuple[ExperimentDigest, ...]
    pareto_experiment_ids: tuple[UUID, ...] = ()
    limitations: tuple[str, ...] = ()


class Finding(DomainModel):
    statement: str
    experiment_ids: tuple[UUID, ...] = ()
    confidence: Literal["low", "medium", "high"]


class Interpretation(DomainModel):
    """An agent's reading of a digest. Advisory only; it changes nothing."""

    schema_version: Literal["1"] = "1"
    author: str
    campaign_id: UUID
    summary: str
    findings: tuple[Finding, ...] = ()
    suggested_next_steps: tuple[str, ...] = ()
    caveats: tuple[str, ...] = ()

    @field_validator("summary")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("an interpretation needs a summary")
        return value


class ResearchAgent(Protocol):
    """Agent-side contract with deliberately no execution capability."""

    @property
    def name(self) -> str: ...

    def draft_campaign(self, goal: ResearchGoal, context: DraftingContext) -> CampaignDraft: ...

    def interpret(self, digest: EvidenceDigest) -> Interpretation: ...
