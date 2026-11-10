"""A deterministic research agent: no language model, no network, no cost.

It is deliberately simple. It shows the shape of the agent boundary and gives a
useful baseline that any model-backed agent has to beat:

- **Drafting** adapts an operator-chosen, known-good template. It renames the
  campaign, sets objectives and budget from the goal, and may only *narrow*
  parameter ranges. Every change is listed in the rationale.
- **Interpreting** reads a digest with plain rules: the best result per
  objective, how much of each parameter range was explored, QC failure patterns
  and contamination flags. Confidence is about evidence volume, not statistics.
"""

from __future__ import annotations

import copy
import re
from collections import Counter
from typing import Any, cast

from autonomous_lab.agents.interface import (
    CampaignDraft,
    DraftingContext,
    EvidenceDigest,
    ExperimentDigest,
    Finding,
    Interpretation,
    ObjectiveHint,
    ResearchGoal,
)
from autonomous_lab.domain.enums import ObjectiveDirection
from autonomous_lab.domain.metric_registry import metric_definition

AGENT_NAME = "autolab-template-agent@1"


class TemplateResearchAgent:
    """Rule-based ``ResearchAgent`` implementation."""

    @property
    def name(self) -> str:
        return AGENT_NAME

    # ---------------------------------------------------------------- draft

    def draft_campaign(self, goal: ResearchGoal, context: DraftingContext) -> CampaignDraft:
        document = copy.deepcopy(context.template)
        changes: list[str] = []
        assumptions: list[str] = [
            "execution settings (devices, clock, replicates, schedule) are copied unchanged "
            "from the operator's template"
        ]
        template_type = document.get("experiment_type", "assay")
        if template_type != goal.experiment_type:
            assumptions.append(
                f"goal asks for a {goal.experiment_type} campaign but the template is "
                f"{template_type}; review will reject this draft"
            )

        campaign = dict(document.get("campaign", {}))
        campaign["name"] = _slug(goal.title)
        changes.append(f"named the campaign {campaign['name']!r}")
        if goal.max_experiments is not None:
            campaign["max_experiments"] = goal.max_experiments
            changes.append(f"set the budget to {goal.max_experiments} experiments")
            optimization = dict(document.get("optimization", {}))
            startup = optimization.get("startup_experiments")
            if isinstance(startup, int) and startup > goal.max_experiments:
                optimization["startup_experiments"] = goal.max_experiments
                document["optimization"] = optimization
                changes.append("reduced startup experiments to fit the budget")
        document["campaign"] = campaign

        if goal.objectives:
            metrics: list[dict[str, str]] = []
            for hint in goal.objectives:
                if hint.metric not in context.available_metrics:
                    assumptions.append(
                        f"ignored objective {hint.metric!r}: this experiment type does not "
                        f"measure it (available: {', '.join(context.available_metrics)})"
                    )
                    continue
                metrics.append(
                    {
                        "name": hint.metric,
                        "direction": hint.direction.value,
                        "unit": metric_definition(hint.metric).canonical_unit,
                    }
                )
            if metrics:
                objective = dict(document.get("objective", {}))
                objective["metrics"] = metrics
                document["objective"] = objective
                changes.append(
                    "objectives: " + ", ".join(f"{m['direction']} {m['name']}" for m in metrics)
                )

        space: dict[str, Any] = copy.deepcopy(document.get("parameter_space", {}))
        for name, (low, high) in goal.parameter_focus.items():
            raw_parameter = space.get(name)
            if not isinstance(raw_parameter, dict):
                assumptions.append(f"ignored focus on unknown parameter {name!r}")
                continue
            parameter = cast(dict[str, Any], raw_parameter)
            current_low = float(parameter["minimum"])
            current_high = float(parameter["maximum"])
            new_low, new_high = max(current_low, low), min(current_high, high)
            if new_low >= new_high:
                assumptions.append(
                    f"ignored focus on {name!r}: [{low}, {high}] does not overlap the "
                    f"template range [{current_low}, {current_high}]"
                )
                continue
            if (new_low, new_high) != (low, high):
                assumptions.append(f"clipped focus on {name!r} to the template range")
            parameter["minimum"], parameter["maximum"] = new_low, new_high
            changes.append(
                f"narrowed {name} to [{new_low}, {new_high}] {parameter.get('unit', '')}"
            )
        document["parameter_space"] = space

        targets = (
            " and ".join(f"{hint.direction.value} {hint.metric}" for hint in goal.objectives)
            or "the template objectives"
        )
        return CampaignDraft(
            author=self.name,
            goal=goal,
            hypothesis=(
                f"Within the searched ranges of {', '.join(sorted(space)) or 'the parameters'}, "
                f"some conditions {targets} better than others, and closed-loop optimisation "
                "will find them within the budget."
            ),
            rationale="Adapted the operator template: " + "; ".join(changes) + ".",
            assumptions=tuple(assumptions),
            document=document,
        )

    # ------------------------------------------------------------ interpret

    def interpret(self, digest: EvidenceDigest) -> Interpretation:
        observed = [item for item in digest.experiments if item.metrics and not item.failed_checks]
        usable = [item for item in observed if not item.contamination_flagged]
        qc_failed = [item for item in digest.experiments if item.state == "qc_failed"]
        execution_failed = [
            item for item in digest.experiments if item.state in {"failed", "aborted"}
        ]
        rejected = [item for item in digest.experiments if item.state == "rejected"]
        findings: list[Finding] = []
        next_steps: list[str] = []
        confidence = _confidence(len(usable))

        for hint in digest.objectives:
            candidates = [item for item in usable if hint.metric in item.metrics]
            if not candidates:
                continue
            best = _best(candidates, hint)
            findings.append(
                Finding(
                    statement=(
                        f"Best {hint.metric} ({hint.direction.value}): "
                        f"{best.metrics[hint.metric]:.4g} at {_describe(best.parameters)} "
                        f"(experiment #{best.ordinal})."
                    ),
                    experiment_ids=(best.experiment_id,),
                    confidence=confidence,
                )
            )

        for name, (low, high) in digest.parameter_ranges.items():
            values = [
                float(item.parameters[name]["value"])
                for item in usable
                if isinstance(item.parameters.get(name), dict)
            ]
            if len(values) < 2 or high <= low:
                continue
            explored = (max(values) - min(values)) / (high - low)
            if explored < 0.5:
                findings.append(
                    Finding(
                        statement=f"Only {explored:.0%} of the {name} range was explored.",
                        confidence="high",
                    )
                )
                next_steps.append(f"Widen exploration of {name} before trusting the optimum.")

        if digest.pareto_experiment_ids:
            findings.append(
                Finding(
                    statement=(
                        f"{len(digest.pareto_experiment_ids)} experiment(s) form the trade-off "
                        "(Pareto) front between the objectives."
                    ),
                    experiment_ids=digest.pareto_experiment_ids,
                    confidence=confidence,
                )
            )
        if qc_failed:
            common = Counter(code for item in qc_failed for code in item.failed_checks)
            top = ", ".join(f"{code} ({count})" for code, count in common.most_common(3))
            findings.append(
                Finding(
                    statement=(
                        f"{len(qc_failed)} of {len(digest.experiments)} experiments failed QC; "
                        f"most common checks: {top or 'unknown'}."
                    ),
                    experiment_ids=tuple(item.experiment_id for item in qc_failed),
                    confidence="high",
                )
            )
            next_steps.append("Inspect the failing QC checks before running more experiments.")
        if execution_failed:
            next_steps.append(
                f"{len(execution_failed)} execution failure(s): check the devices and the "
                "campaign's pause reason."
            )
        if len(usable) < 5:
            next_steps.append("Run more experiments; fewer than five usable observations so far.")

        caveats = [
            "Rule-based summary: no statistical model, no significance testing.",
            *digest.limitations,
        ]
        flagged = [item for item in digest.experiments if item.contamination_flagged]
        if flagged:
            caveats.append(
                f"{len(flagged)} run(s) flagged as contaminated are excluded here; the optimiser "
                "already learned from them."
            )
        summary = (
            f"{digest.campaign_name} ({digest.experiment_type}, {digest.state}): "
            f"{len(digest.experiments)} experiments, {len(usable)} usable observations, "
            f"{len(qc_failed)} QC failures, {len(execution_failed)} execution failures, "
            f"{len(rejected)} rejected."
        )
        return Interpretation(
            author=self.name,
            campaign_id=digest.campaign_id,
            summary=summary,
            findings=tuple(findings),
            suggested_next_steps=tuple(next_steps),
            caveats=tuple(caveats),
        )


def _best(candidates: list[ExperimentDigest], hint: ObjectiveHint) -> ExperimentDigest:
    if hint.direction is ObjectiveDirection.MAXIMIZE:
        return max(candidates, key=lambda item: item.metrics[hint.metric])
    return min(candidates, key=lambda item: item.metrics[hint.metric])


def _confidence(observations: int) -> Any:
    if observations >= 10:
        return "high"
    if observations >= 5:
        return "medium"
    return "low"


def _describe(parameters: dict[str, Any]) -> str:
    parts: list[str] = []
    for name, value in sorted(parameters.items()):
        if isinstance(value, dict):
            value = cast(dict[str, Any], value)
            parts.append(f"{name}={float(value['value']):.4g} {value.get('unit', '')}".strip())
        else:
            parts.append(f"{name}={value}")
    return ", ".join(parts)


def _slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
    return slug[:60] or "agent_drafted_campaign"
