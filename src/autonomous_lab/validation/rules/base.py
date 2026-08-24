"""Shared validation-rule context and issue constructor."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from autonomous_lab.campaigns.config import CampaignConfig, ValidationPolicyConfig
from autonomous_lab.domain.enums import IssueSeverity
from autonomous_lab.domain.experiments import ExperimentProposal
from autonomous_lab.domain.protocols import (
    ExperimentProtocol,
    ValidationIssue,
)
from autonomous_lab.hardware.capabilities import CapabilitySnapshot


@dataclass(frozen=True, slots=True)
class ValidationContext:
    proposal: ExperimentProposal
    protocol: ExperimentProtocol
    campaign: CampaignConfig
    policy: ValidationPolicyConfig
    capability_snapshot: CapabilitySnapshot
    historical_parameter_hashes: frozenset[str]
    validation_time: datetime


def error(
    code: str,
    path: str,
    message: str,
    rule_name: str,
    *,
    details: dict[str, Any] | None = None,
) -> ValidationIssue:
    return ValidationIssue(
        code=code,
        severity=IssueSeverity.ERROR,
        path=path,
        message=message,
        rule_name=rule_name,
        rule_version="1",
        details={} if details is None else details,
    )
