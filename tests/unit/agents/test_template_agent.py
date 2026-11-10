"""The rule-based research agent and the draft review gate."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from autonomous_lab.agents import (
    EvidenceDigest,
    ExperimentDigest,
    ObjectiveHint,
    ResearchGoal,
    TemplateResearchAgent,
)
from autonomous_lab.campaigns.agent_gateway import drafting_context, review_draft
from autonomous_lab.campaigns.loader import load_culture_policy
from autonomous_lab.domain.enums import ObjectiveDirection

ROOT = Path(__file__).resolve().parents[3]
TEMPLATE: dict[str, Any] = yaml.safe_load(
    (ROOT / "configs" / "culture-algae-sim.yaml").read_text(encoding="utf-8")
)
POLICY = load_culture_policy(ROOT / "configs" / "culture-policy.yaml")


def _goal(**overrides: Any) -> ResearchGoal:
    values: dict[str, Any] = {
        "title": "Low-energy growth near 28 C",
        "statement": "Find conditions that grow well without much heating or light.",
        "experiment_type": "culture",
        "objectives": [
            {"metric": "final_od750", "direction": "maximize"},
            {"metric": "energy_kwh", "direction": "minimize"},
        ],
        "max_experiments": 12,
        "parameter_focus": {"temperature_setpoint": [24, 31]},
    }
    values.update(overrides)
    return ResearchGoal.model_validate(values, strict=False)


def _draft(goal: ResearchGoal | None = None):
    return TemplateResearchAgent().draft_campaign(
        goal or _goal(), drafting_context(TEMPLATE, POLICY)
    )


def test_draft_adapts_the_template_and_passes_review() -> None:
    draft = _draft()
    space = draft.document["parameter_space"]["temperature_setpoint"]
    assert (space["minimum"], space["maximum"]) == (24.0, 31.0)
    assert draft.document["campaign"]["max_experiments"] == 12
    assert draft.document["optimization"]["startup_experiments"] == 8
    assert draft.document["execution"] == TEMPLATE["execution"]  # never touched
    review = review_draft(draft, policy=POLICY, template=TEMPLATE)
    assert review.accepted, review.errors
    assert review.warnings == ()
    assert review.campaign_yaml is not None
    assert "# Drafted by: autolab-template-agent@1" in review.campaign_yaml
    assert review.draft_sha256 in review.campaign_yaml


def test_focus_can_only_narrow_and_unknown_metrics_are_dropped() -> None:
    draft = _draft(
        _goal(
            parameter_focus={"temperature_setpoint": [10, 25], "ph": [6, 8]},
            objectives=[{"metric": "cost_per_test", "direction": "minimize"}],
        )
    )
    space = draft.document["parameter_space"]["temperature_setpoint"]
    assert space["minimum"] == 20.0 and space["maximum"] == 25.0  # clipped to the template
    assert draft.document["objective"] == TEMPLATE["objective"]
    notes = " ".join(draft.assumptions)
    assert "clipped" in notes and "'ph'" in notes and "cost_per_test" in notes


def test_review_rejects_ranges_outside_the_trusted_policy() -> None:
    draft = _draft()
    hot = dict(draft.document)
    hot["parameter_space"] = {
        **draft.document["parameter_space"],
        "temperature_setpoint": {
            **draft.document["parameter_space"]["temperature_setpoint"],
            "maximum": 39,
        },
    }
    review = review_draft(
        draft.model_copy(update={"document": hot}), policy=POLICY, template=TEMPLATE
    )
    assert not review.accepted
    assert any("exceeds the installation policy" in item for item in review.errors)
    assert any("widens parameter ranges" in item for item in review.warnings)
    assert review.campaign_yaml is None


def test_review_flags_execution_changes_for_a_person() -> None:
    draft = _draft()
    document = dict(draft.document)
    document["execution"] = {**draft.document["execution"], "on_interrupt": "keep_holding"}
    review = review_draft(
        draft.model_copy(update={"document": document}), policy=POLICY, template=TEMPLATE
    )
    assert review.accepted  # valid, but a person must sign off
    assert any("'execution'" in item for item in review.warnings)


def test_review_rejects_a_draft_that_does_not_match_its_goal_or_schema() -> None:
    wrong_type = _draft().model_copy(update={"goal": _goal(experiment_type="assay")})
    assert any(
        "goal asked for assay" in item for item in review_draft(wrong_type, policy=POLICY).errors
    )
    broken = _draft().model_copy(update={"document": {"experiment_type": "culture"}})
    review = review_draft(broken, policy=POLICY)
    assert not review.accepted and "failed validation" in review.errors[0]


def _digest() -> EvidenceDigest:
    def experiment(ordinal: int, od: float | None, temp: float, **kw: Any) -> ExperimentDigest:
        return ExperimentDigest(
            ordinal=ordinal,
            experiment_id=uuid4(),
            state=kw.pop("state", "observed"),
            parameters={"temperature_setpoint": {"value": temp, "unit": "degC"}},
            metrics={} if od is None else {"final_od750": od, "energy_kwh": 1.0},
            **kw,
        )

    return EvidenceDigest(
        campaign_id=uuid4(),
        campaign_name="demo",
        experiment_type="culture",
        state="completed",
        objectives=(ObjectiveHint(metric="final_od750", direction=ObjectiveDirection.MAXIMIZE),),
        parameter_ranges={"temperature_setpoint": (20.0, 34.0)},
        experiments=(
            experiment(0, 1.1, 27.0),
            experiment(1, 1.4, 28.0),
            experiment(2, 9.0, 29.0, contamination_flagged=True),
            experiment(3, None, 30.0, state="qc_failed", failed_checks=("temperature_control",)),
        ),
        limitations=("Simulated culture.",),
    )


def test_interpretation_is_grounded_in_the_digest() -> None:
    interpretation = TemplateResearchAgent().interpret(_digest())
    statements = " ".join(item.statement for item in interpretation.findings)
    # The contaminated run's high OD is excluded from "best".
    assert "Best final_od750 (maximize): 1.4" in statements
    assert "#1" in statements
    assert "Only 7% of the temperature_setpoint range was explored" in statements
    assert "temperature_control (1)" in statements
    assert any("contaminated" in item for item in interpretation.caveats)
    assert "Simulated culture." in interpretation.caveats
    assert all(item.confidence in {"low", "high"} for item in interpretation.findings)
