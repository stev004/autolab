"""Trusted protocol finalization, validation, and execution-receipt verification."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime

from autonomous_lab.campaigns.config import CampaignConfig, ValidationPolicyConfig
from autonomous_lab.domain.enums import IssueSeverity, ValidationStatus
from autonomous_lab.domain.experiments import ExperimentProposal
from autonomous_lab.domain.identifiers import require_utc, utc_now
from autonomous_lab.domain.protocols import (
    ExecutableProtocol,
    ExperimentProtocol,
    ValidationIssue,
    ValidationReport,
)
from autonomous_lab.hardware.capabilities import CapabilitySnapshot
from autonomous_lab.provenance.hashing import canonical_sha256
from autonomous_lab.validation.reports import ReceiptVerificationError, ValidationOutcome
from autonomous_lab.validation.rules import (
    binding,
    capabilities,
    controls,
    operation_order,
    parameters,
    structure,
    volumes,
)
from autonomous_lab.validation.rules.base import ValidationContext, error


class ProtocolValidationService:
    """The only application service that issues executable protocol receipts."""

    def __init__(self, *, clock: Callable[[], datetime] = utc_now) -> None:
        self._clock = clock

    def validate(
        self,
        *,
        proposal: ExperimentProposal,
        protocol: ExperimentProtocol,
        campaign: CampaignConfig,
        policy: ValidationPolicyConfig,
        capability_snapshot: CapabilitySnapshot,
        historical_parameter_hashes: Iterable[str] = (),
    ) -> ValidationOutcome:
        validated_at = require_utc(self._clock())
        protocol_hash = calculate_protocol_hash(protocol)
        preliminary_issues: tuple[ValidationIssue, ...] = ()
        if protocol.protocol_hash is not None and protocol.protocol_hash != protocol_hash:
            preliminary_issues = (
                error(
                    "protocol.preexisting_hash_mismatch",
                    "protocol_hash",
                    "protocol content does not match its pre-existing hash",
                    "protocol_integrity",
                    details={"expected": protocol_hash, "actual": protocol.protocol_hash},
                ),
            )
        finalized = protocol.model_copy(update={"protocol_hash": protocol_hash})
        context = ValidationContext(
            proposal=proposal,
            protocol=finalized,
            campaign=campaign,
            policy=policy,
            capability_snapshot=capability_snapshot,
            historical_parameter_hashes=frozenset(historical_parameter_hashes),
            validation_time=validated_at,
        )
        rule_issues = (
            parameters.validate(context)
            + binding.validate(context)
            + structure.validate(context)
            + controls.validate(context)
            + volumes.validate(context)
            + operation_order.validate(context)
            + capabilities.validate_policy_measurement(context)
            + capabilities.validate(context)
        )
        issues = preliminary_issues + rule_issues
        rejected = any(issue.severity is IssueSeverity.ERROR for issue in issues)
        status = ValidationStatus.REJECTED if rejected else ValidationStatus.PASSED
        policy_hash = canonical_sha256(policy)
        capability_hash = capability_snapshot.sha256
        report = ValidationReport(
            protocol_id=finalized.protocol_id,
            protocol_hash=protocol_hash,
            capability_snapshot_hash=capability_hash,
            validation_policy_hash=policy_hash,
            status=status,
            issues=issues,
            validated_at=validated_at,
        )
        executable = None
        if not rejected:
            executable = ExecutableProtocol(
                protocol=finalized,
                validation_report_id=report.report_id,
                protocol_hash=protocol_hash,
                capability_snapshot_hash=capability_hash,
                validation_policy_hash=policy_hash,
                issued_at=validated_at,
            )
        return ValidationOutcome(protocol=finalized, report=report, executable=executable)

    def verify_executable(
        self,
        executable: ExecutableProtocol,
        *,
        report: ValidationReport,
        policy: ValidationPolicyConfig,
        capability_snapshot: CapabilitySnapshot,
    ) -> ExperimentProtocol:
        """Verify the exact current artifacts immediately before device dispatch."""

        issues: list[ValidationIssue] = []
        actual_protocol_hash = calculate_protocol_hash(executable.protocol)
        expected_policy_hash = canonical_sha256(policy)
        expected_capability_hash = capability_snapshot.sha256
        checks = (
            (
                report.status is ValidationStatus.PASSED,
                "receipt.report_not_passed",
                "referenced validation report did not pass",
            ),
            (
                executable.validation_report_id == report.report_id,
                "receipt.report_id_mismatch",
                "executable references a different validation report",
            ),
            (
                executable.protocol.protocol_id == report.protocol_id,
                "receipt.protocol_id_mismatch",
                "validation report belongs to a different protocol",
            ),
            (
                executable.protocol_hash == actual_protocol_hash
                and executable.protocol.protocol_hash == actual_protocol_hash
                and report.protocol_hash == actual_protocol_hash,
                "receipt.protocol_hash_mismatch",
                "protocol content or receipt hash changed after validation",
            ),
            (
                executable.validation_policy_hash == expected_policy_hash
                and report.validation_policy_hash == expected_policy_hash,
                "receipt.policy_hash_mismatch",
                "trusted validation policy changed after validation",
            ),
            (
                executable.capability_snapshot_hash == expected_capability_hash
                and report.capability_snapshot_hash == expected_capability_hash,
                "receipt.capability_hash_mismatch",
                "hardware capabilities changed after validation",
            ),
        )
        for passed, code, message in checks:
            if not passed:
                issues.append(error(code, "receipt", message, "receipt_integrity"))
        if issues:
            raise ReceiptVerificationError(tuple(issues))
        return executable.protocol


def calculate_protocol_hash(protocol: ExperimentProtocol) -> str:
    """Hash all protocol content except the circular embedded hash field."""

    return canonical_sha256(protocol, exclude={"protocol_hash"})
