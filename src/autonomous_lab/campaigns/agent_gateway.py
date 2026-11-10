"""The application side of the research-agent boundary (stage 7).

Agents live in ``autonomous_lab.agents`` and can only produce documents. This
gateway is the one place that connects them to the lab, and it does so in two
narrow, one-directional ways:

- **out**: ``build_digest`` turns the evidence ledger into a read-only
  ``EvidenceDigest`` (values and statuses only), and ``drafting_context`` hands
  an agent a template and the trusted policy limits to draft against;
- **in**: ``review_draft`` checks an untrusted ``CampaignDraft`` with the same
  strict config validation as any hand-written campaign, plus agent-specific
  checks. Accepting a draft produces campaign YAML for a *person* to create and
  run. Nothing here creates a campaign, runs an experiment or touches a device.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import yaml
from sqlalchemy import select

from autonomous_lab.agents import (
    CampaignDraft,
    DraftingContext,
    EvidenceDigest,
    ExperimentDigest,
    ObjectiveHint,
)
from autonomous_lab.campaigns.config import CampaignConfig, ObjectiveConfig, ValidationPolicyConfig
from autonomous_lab.campaigns.culture_config import CULTURE_METRICS, CultureCampaignConfig
from autonomous_lab.campaigns.culture_service import CONTAMINATION_EVENT
from autonomous_lab.campaigns.lifecycle import campaign_status
from autonomous_lab.campaigns.loader import ConfigurationLoadError, validate_campaign_document
from autonomous_lab.culture.models import CulturePolicy
from autonomous_lab.domain.metric_registry import METRIC_REGISTRY
from autonomous_lab.domain.quantities import QuantityValue
from autonomous_lab.provenance.hashing import canonical_sha256
from autonomous_lab.storage import Storage
from autonomous_lab.storage.orm import (
    AuditEventRecord,
    DerivedMetricRecord,
    ProposalRecord,
    QCCheckRecord,
    QCRunRecord,
)

#: Sections of a campaign document that decide *how* experiments are executed.
#: An agent may propose changes, but review flags them for explicit human sign-off.
EXECUTION_SECTIONS = ("execution", "simulation", "culture_design", "assay_design")

# v1 culture parameter names -> the policy range that bounds them.
_CULTURE_POLICY_RANGES = {
    "temperature_setpoint": "temperature_setpoint_range",
    "light_intensity": "light_intensity_range",
}


@dataclass(frozen=True, slots=True)
class DraftReview:
    """The verdict on one draft. ``errors`` block it; ``warnings`` need a person's attention."""

    accepted: bool
    experiment_type: str
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    draft_sha256: str
    config_sha256: str | None
    campaign_yaml: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "experiment_type": self.experiment_type,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "draft_sha256": self.draft_sha256,
            "config_sha256": self.config_sha256,
        }


def drafting_context(
    template: dict[str, Any], policy: CulturePolicy | ValidationPolicyConfig
) -> DraftingContext:
    """What an agent may see while drafting: a template, the metric list and the limits."""

    culture = template.get("experiment_type") == "culture"
    metrics = sorted(CULTURE_METRICS) if culture else sorted(METRIC_REGISTRY)
    return DraftingContext(
        template=template,
        available_metrics=tuple(metrics),
        policy_limits=policy.model_dump(mode="json"),
    )


def review_draft(
    draft: CampaignDraft,
    *,
    policy: CulturePolicy | ValidationPolicyConfig,
    template: dict[str, Any] | None = None,
) -> DraftReview:
    """Validate an untrusted draft exactly like a hand-written campaign, and more."""

    errors: list[str] = []
    warnings: list[str] = []
    document = draft.document
    experiment_type = str(document.get("experiment_type", "assay"))
    draft_sha = canonical_sha256(draft)
    if experiment_type != draft.goal.experiment_type:
        errors.append(
            f"draft is a {experiment_type} campaign but the goal asked for "
            f"{draft.goal.experiment_type}"
        )
    config: CampaignConfig | CultureCampaignConfig | None = None
    try:
        config = validate_campaign_document(document, source=f"draft by {draft.author}")
    except ConfigurationLoadError as exc:
        errors.append(str(exc))

    if isinstance(config, CultureCampaignConfig):
        if not isinstance(policy, CulturePolicy):
            errors.append("a culture draft must be reviewed against the culture policy")
        else:
            errors += _culture_policy_errors(config, policy)
    elif isinstance(config, CampaignConfig) and not isinstance(policy, ValidationPolicyConfig):
        errors.append("an assay draft must be reviewed against the assay validation policy")

    if template is not None:
        for section in EXECUTION_SECTIONS:
            if document.get(section) != template.get(section):
                warnings.append(
                    f"draft changes the {section!r} section of the template; execution changes "
                    "need explicit human sign-off"
                )
        widened = _widened_parameters(document, template)
        if widened:
            warnings.append(f"draft widens parameter ranges beyond the template: {widened}")

    accepted = not errors and config is not None
    campaign_yaml = None
    config_sha = None
    if accepted:
        assert config is not None
        config_sha = canonical_sha256(config)
        campaign_yaml = _campaign_yaml(draft, draft_sha)
    return DraftReview(
        accepted=accepted,
        experiment_type=experiment_type,
        errors=tuple(errors),
        warnings=tuple(warnings),
        draft_sha256=draft_sha,
        config_sha256=config_sha,
        campaign_yaml=campaign_yaml,
    )


def build_digest(storage: Storage, campaign_id: UUID) -> EvidenceDigest:
    """Summarise one campaign's evidence as values and statuses only."""

    with storage.unit_of_work() as uow:
        campaign = uow.campaigns.get(str(campaign_id))
        config_json = dict(campaign.config_json)
        experiments = uow.experiments.list_for_campaign(str(campaign_id))
        assert uow.session is not None
        ids = [item.id for item in experiments]
        proposals = {
            experiment_id: snapshot
            for experiment_id, snapshot in uow.session.execute(
                select(ProposalRecord.experiment_id, ProposalRecord.snapshot).where(
                    ProposalRecord.experiment_id.in_(ids)
                )
            )
        }
        metrics: dict[str, dict[str, float]] = {}
        for experiment_id, name, value in uow.session.execute(
            select(
                DerivedMetricRecord.experiment_id,
                DerivedMetricRecord.name,
                DerivedMetricRecord.value,
            ).where(DerivedMetricRecord.experiment_id.in_(ids))
        ):
            metrics.setdefault(experiment_id, {})[name] = value
        failed: dict[str, list[str]] = {}
        for experiment_id, name in uow.session.execute(
            select(QCRunRecord.experiment_id, QCCheckRecord.name)
            .join(QCCheckRecord, QCCheckRecord.qc_run_id == QCRunRecord.id)
            .where(QCRunRecord.experiment_id.in_(ids), QCCheckRecord.status == "failed")
        ):
            codes = failed.setdefault(experiment_id, [])
            if name not in codes:
                codes.append(name)
        flagged = set(
            uow.session.scalars(
                select(AuditEventRecord.experiment_id).where(
                    AuditEventRecord.campaign_id == str(campaign_id),
                    AuditEventRecord.event_type == CONTAMINATION_EVENT,
                )
            )
        )
        rows = [(item.ordinal, item.id, item.state, item.failure_message) for item in experiments]
        name = campaign.name
        state = campaign.state
        uow.commit()

    objective = ObjectiveConfig.model_validate(config_json["objective"], strict=False)
    status = campaign_status(storage, campaign_id, objective)
    experiment_type = "culture" if config_json.get("experiment_type") == "culture" else "assay"
    digests = tuple(
        ExperimentDigest(
            ordinal=ordinal,
            experiment_id=UUID(experiment_id),
            state=experiment_state,
            parameters=_parameters(proposals.get(experiment_id, {})),
            metrics=metrics.get(experiment_id, {}),
            failed_checks=tuple(failed.get(experiment_id, [])),
            failure_reason=failure,
            contamination_flagged=experiment_id in flagged,
        )
        for ordinal, experiment_id, experiment_state, failure in sorted(rows)
    )
    return EvidenceDigest(
        campaign_id=campaign_id,
        campaign_name=name,
        experiment_type=experiment_type,
        state=state,
        objectives=tuple(
            ObjectiveHint(metric=metric.name, direction=metric.direction)
            for metric in objective.metrics
        ),
        parameter_ranges={
            key: (float(value["minimum"]), float(value["maximum"]))
            for key, value in _parameter_space(config_json).items()
        },
        experiments=digests,
        pareto_experiment_ids=tuple(
            UUID(str(item["experiment_id"])) for item in status.pareto_frontier
        ),
        limitations=_limitations(config_json, experiment_type),
    )


def _parameter_space(config_json: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Stored configs nest parameters under ``parameters``; YAML documents do not."""

    space = cast(dict[str, Any], config_json["parameter_space"])
    return cast(dict[str, dict[str, Any]], space.get("parameters", space))


def _parameters(snapshot: dict[str, Any]) -> dict[str, Any]:
    values = cast(dict[str, dict[str, Any]], snapshot.get("parameter_values", {}))
    return {
        name: {"value": float(item["value"]), "unit": item["unit"]} for name, item in values.items()
    }


def _limitations(config_json: dict[str, Any], experiment_type: str) -> tuple[str, ...]:
    notes: list[str] = []
    if experiment_type == "culture":
        execution = cast(dict[str, Any], config_json.get("execution", {}))
        if execution.get("clock") == "simulated":
            notes.append("Simulated culture: an illustrative, unvalidated growth model.")
        notes.append("Culture QC thresholds and contamination screens are illustrative.")
        design = cast(dict[str, Any], config_json.get("culture_design", {}))
        reactors = cast(list[Any], design.get("reactor_ids", design.get("reactor_id", [])))
        if len(reactors) > 1:
            notes.append("Culture metrics are means over QC-passed biological replicates.")
    else:
        execution = cast(dict[str, Any], config_json.get("execution", {}))
        if execution.get("mode", "simulation") == "simulation":
            notes.append("Simulated assay: an illustrative, unvalidated response model.")
    return tuple(notes)


def _culture_policy_errors(config: CultureCampaignConfig, policy: CulturePolicy) -> list[str]:
    """Catch ranges the trusted policy would reject for every experiment, before any run."""

    errors: list[str] = []
    for name, field in _CULTURE_POLICY_RANGES.items():
        parameter = config.parameter_space.parameters.get(name)
        if parameter is None:
            continue
        allowed = getattr(policy, field)
        unit = allowed.minimum.unit
        low = QuantityValue(value=parameter.minimum, unit=parameter.unit).to(unit).value
        high = QuantityValue(value=parameter.maximum, unit=parameter.unit).to(unit).value
        minimum = allowed.minimum.value
        maximum = allowed.maximum.to(unit).value
        if low < minimum or high > maximum:
            errors.append(
                f"{name} range [{low}, {high}] {unit} exceeds the installation policy "
                f"[{minimum}, {maximum}]"
            )
    return errors


def _widened_parameters(document: dict[str, Any], template: dict[str, Any]) -> list[str]:
    widened: list[str] = []
    proposed = cast(dict[str, dict[str, Any]], document.get("parameter_space", {}))
    original = cast(dict[str, dict[str, Any]], template.get("parameter_space", {}))
    for name, parameter in proposed.items():
        base = original.get(name)
        if base is None:
            widened.append(f"{name} (new parameter)")
            continue
        try:
            if float(parameter["minimum"]) < float(base["minimum"]) or float(
                parameter["maximum"]
            ) > float(base["maximum"]):
                widened.append(name)
        except (KeyError, TypeError, ValueError):
            widened.append(f"{name} (unreadable range)")
    return widened


def _campaign_yaml(draft: CampaignDraft, draft_sha: str) -> str:
    """Campaign YAML whose header comments keep the draft's provenance.

    The header is part of the campaign source text, so ``create`` stores it (and
    its hash) with the campaign: which agent proposed it and from which draft.
    """

    header = [
        f"# Drafted by: {draft.author}",
        f"# Goal: {draft.goal.title}",
        f"# Draft SHA-256: {draft_sha}",
        f"# Reviewed: {datetime.now(UTC).isoformat(timespec='seconds')}",
        "# Accepted by review; a person must still create and run this campaign.",
    ]
    body = yaml.safe_dump(draft.document, sort_keys=False)
    return "\n".join(header) + "\n" + body
