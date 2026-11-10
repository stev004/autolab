"""Deterministic validation for culture protocols and issuing of executable receipts.

Three layers must all agree before anything is sent to a reactor:

1. *binding*: the protocol is exactly what the trusted compiler produces for this
   proposal and design (it is recompiled and compared, not trusted);
2. *policy*: the installation's trusted limits, which a campaign cannot widen;
3. *capability*: the limits the controller itself published in its manifest.

Every problem becomes a ``ValidationIssue`` with a stable code. Any error rejects
the protocol; only a clean report yields an ``ExecutableCultureProtocol``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from autonomous_lab.culture.compiler import CultureCompilationError, CultureProtocolCompiler
from autonomous_lab.culture.models import (
    CultureControllerCapabilities,
    CultureDesign,
    CulturePolicy,
    CultureProtocol,
    CultureReplicate,
    ExecutableCultureProtocol,
    interval_unit,
    seconds,
)
from autonomous_lab.domain.enums import IssueSeverity, ValidationStatus
from autonomous_lab.domain.experiments import ExperimentProposal
from autonomous_lab.domain.protocols import QuantityRange, ValidationIssue, ValidationReport
from autonomous_lab.domain.quantities import QuantityValue
from autonomous_lab.provenance.hashing import canonical_sha256

RULE_VERSION = "1"


@dataclass(frozen=True, slots=True)
class CultureValidationResult:
    protocol: CultureProtocol
    report: ValidationReport
    executable: ExecutableCultureProtocol | None


class CultureValidationService:
    """The only issuer of culture execution receipts."""

    def __init__(self, compiler: CultureProtocolCompiler | None = None) -> None:
        self.compiler = compiler or CultureProtocolCompiler()

    def validate(
        self,
        *,
        proposal: ExperimentProposal,
        protocol: CultureProtocol,
        design: CultureDesign,
        policy: CulturePolicy,
        capabilities: CultureControllerCapabilities,
    ) -> CultureValidationResult:
        issues = [
            *self._binding(proposal, protocol, design, policy),
            *_policy(protocol, policy),
            *_capability(protocol, policy, capabilities),
        ]
        protocol_hash = protocol.compute_hash()
        failed = any(issue.severity is IssueSeverity.ERROR for issue in issues)
        report = ValidationReport(
            protocol_id=protocol.protocol_id,
            protocol_hash=protocol_hash,
            capability_snapshot_hash=capabilities.sha256,
            validation_policy_hash=policy.sha256,
            status=ValidationStatus.REJECTED if failed else ValidationStatus.PASSED,
            issues=tuple(issues),
        )
        executable = None
        if not failed:
            executable = ExecutableCultureProtocol(
                protocol=protocol,
                validation_report_id=report.report_id,
                protocol_hash=protocol_hash,
                capability_snapshot_hash=capabilities.sha256,
                validation_policy_hash=policy.sha256,
            )
        return CultureValidationResult(protocol=protocol, report=report, executable=executable)

    def _binding(
        self,
        proposal: ExperimentProposal,
        protocol: CultureProtocol,
        design: CultureDesign,
        policy: CulturePolicy,
    ) -> list[ValidationIssue]:
        rule = "culture_binding"
        issues: list[ValidationIssue] = []
        identity = f"{protocol.compiler_name}@{protocol.compiler_version}"
        if identity not in policy.trusted_compilers:
            issues.append(
                _error("untrusted_compiler", "compiler", rule, f"{identity} is not trusted")
            )
        if protocol.protocol_hash != protocol.compute_hash():
            issues.append(
                _error("protocol_hash_mismatch", "protocol_hash", rule, "protocol was modified")
            )
        if protocol.experiment_id != proposal.experiment_id:
            issues.append(_error("experiment_mismatch", "experiment_id", rule, "wrong experiment"))
        if protocol.source_proposal_hash != canonical_sha256(proposal):
            issues.append(
                _error("proposal_hash_mismatch", "source_proposal_hash", rule, "proposal differs")
            )
        if protocol.design_hash != canonical_sha256(design):
            issues.append(_error("design_hash_mismatch", "design_hash", rule, "design differs"))
        try:
            expected = self.compiler.compile(proposal, design)
        except CultureCompilationError as exc:
            issues.append(_error("uncompilable_proposal", "parameter_values", rule, str(exc)))
            return issues
        # Compare the scientific content, not per-instance IDs and timestamps.
        if [item.hold for item in protocol.replicates] != [
            item.hold for item in expected.replicates
        ]:
            issues.append(
                _error(
                    "hold_not_from_proposal",
                    "replicates[].hold",
                    rule,
                    "hold conditions or reactors differ from what the proposal compiles to",
                )
            )
        if [item.measurement_plan for item in protocol.replicates] != [
            item.measurement_plan for item in expected.replicates
        ]:
            issues.append(
                _error(
                    "plan_not_from_design",
                    "replicates[].measurement_plan",
                    rule,
                    "sampling plan differs from what the design compiles to",
                )
            )
        if protocol.telemetry_interval != expected.telemetry_interval:
            issues.append(
                _error(
                    "plan_not_from_design",
                    "telemetry_interval",
                    rule,
                    "telemetry interval differs from the design",
                )
            )
        if protocol.minimum_passing_replicates != expected.minimum_passing_replicates:
            issues.append(
                _error(
                    "replicate_rule_not_from_design",
                    "minimum_passing_replicates",
                    rule,
                    "replicate acceptance rule differs from the design",
                )
            )
        if protocol.parameter_values != proposal.parameter_values:
            issues.append(
                _error("parameter_snapshot_mismatch", "parameter_values", rule, "values differ")
            )
        return issues


def _policy(protocol: CultureProtocol, policy: CulturePolicy) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if len(protocol.replicates) > policy.maximum_replicates:
        issues.append(
            _error(
                "too_many_replicates",
                "replicates",
                "culture_policy",
                f"{len(protocol.replicates)} replicates exceed {policy.maximum_replicates}",
            )
        )
    for replicate in protocol.replicates:
        issues += _prefixed(_policy_for(replicate, policy), replicate)
    return _unique(issues)


def _capability(
    protocol: CultureProtocol,
    policy: CulturePolicy,
    capabilities: CultureControllerCapabilities,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for replicate in protocol.replicates:
        issues += _prefixed(_capability_for(replicate, policy, capabilities), replicate)
    return _unique(issues)


def _prefixed(issues: list[ValidationIssue], replicate: CultureReplicate) -> list[ValidationIssue]:
    return [
        issue.model_copy(update={"path": f"replicates[{replicate.replicate_index}].{issue.path}"})
        for issue in issues
    ]


def _unique(issues: list[ValidationIssue]) -> list[ValidationIssue]:
    """Report a condition-level problem once, not once per identical replicate."""

    seen: set[str] = set()
    result: list[ValidationIssue] = []
    for issue in issues:
        condition_level = issue.code != "unknown_reactor"
        # Strip the "replicates[i]." prefix so one problem per variable is reported.
        variable_path = issue.path.split("].", 1)[-1]
        key = f"{issue.code}:{variable_path}" if condition_level else f"{issue.code}:{issue.path}"
        if key not in seen:
            seen.add(key)
            result.append(issue)
    return result


def _policy_for(replicate: CultureReplicate, policy: CulturePolicy) -> list[ValidationIssue]:
    rule = "culture_policy"
    hold = replicate.hold
    issues: list[ValidationIssue] = []
    for name, value in hold.setpoints.items():
        path = f"hold.setpoints.{name}"
        limit = policy.setpoint_limits.get(name)
        if limit is None:
            issues.append(
                _error(
                    "setpoint_not_allowed_by_policy",
                    path,
                    rule,
                    f"the installation policy does not allow setting {name!r}",
                )
            )
            continue
        issues += _range_issues(
            value, limit, limit.minimum.unit, code="setpoint_outside_policy", path=path, rule=rule
        )
    if seconds(hold.duration) > seconds(policy.maximum_duration):
        issues.append(
            _error("duration_exceeds_policy", "hold.duration", rule, "hold is longer than allowed")
        )
    points = replicate.measurement_plan.points
    if len(points) > policy.maximum_sample_points:
        issues.append(
            _error(
                "too_many_sample_points",
                "measurement_plan.points",
                rule,
                f"{len(points)} points exceed {policy.maximum_sample_points}",
            )
        )
    previous = Decimal(0)
    for index, point in enumerate(points):
        offset = seconds(point.offset)
        if offset - previous < seconds(policy.minimum_sampling_interval):
            issues.append(
                _error(
                    "sampling_too_frequent",
                    f"measurement_plan.points[{index}]",
                    rule,
                    "readings are closer together than policy allows",
                )
            )
            break
        previous = offset
    if points and seconds(points[-1].offset) != seconds(hold.duration):
        issues.append(
            _error(
                "missing_end_point",
                "measurement_plan.points",
                rule,
                "the final reading must coincide with the end of the hold",
            )
        )
    return issues


def _capability_for(
    replicate: CultureReplicate,
    policy: CulturePolicy,
    capabilities: CultureControllerCapabilities,
) -> list[ValidationIssue]:
    rule = "culture_capability"
    hold = replicate.hold
    issues: list[ValidationIssue] = []
    if hold.reactor_id not in capabilities.reactor_ids:
        issues.append(
            _error("unknown_reactor", "hold.reactor_id", rule, f"no reactor {hold.reactor_id!r}")
        )
    declared = set(capabilities.controlled_variables)
    requested = set(hold.setpoints)
    if declared != requested:
        # Exact key set: the device has nothing to hold a missing variable at.
        issues.append(
            _error(
                "setpoints_do_not_match_device",
                "hold.setpoints",
                rule,
                "setpoints must name exactly the device's controlled variables",
                missing=sorted(declared - requested),
                unknown=sorted(requested - declared),
            )
        )
    for name, value in hold.setpoints.items():
        variable = capabilities.controlled_variables.get(name)
        if variable is None:
            continue
        path = f"hold.setpoints.{name}"
        issues += _range_issues(
            value,
            variable.range,
            variable.unit,
            code="setpoint_outside_device_range",
            path=path,
            rule=rule,
        )
        limit = capabilities.protection_limits.get(name)
        if limit is None:
            continue
        unit = variable.unit
        margin_quantity = policy.minimum_protection_margins.get(name)
        margin = (
            Decimal(0) if margin_quantity is None else margin_quantity.to(interval_unit(unit)).value
        )
        magnitude = value.to(unit).value
        too_low = limit.minimum is not None and magnitude - limit.minimum.to(unit).value < margin
        too_high = limit.maximum is not None and limit.maximum.to(unit).value - magnitude < margin
        if too_low or too_high:
            issues.append(
                _error(
                    "setpoint_too_close_to_protection_limit",
                    path,
                    rule,
                    f"{name} setpoint must stay {margin} {interval_unit(unit)} inside the "
                    "device's protection limits",
                    value=str(magnitude),
                    minimum=None if limit.minimum is None else str(limit.minimum.to(unit).value),
                    maximum=None if limit.maximum is None else str(limit.maximum.to(unit).value),
                )
            )
    if seconds(hold.duration) > seconds(capabilities.maximum_hold_duration):
        issues.append(
            _error("duration_exceeds_device", "hold.duration", rule, "device cannot hold that long")
        )
    return issues


def _range_issues(
    value: QuantityValue,
    allowed: QuantityRange,
    unit: str,
    *,
    code: str,
    path: str,
    rule: str,
) -> list[ValidationIssue]:
    # Resolution is an interval: temperature steps are delta_degree_Celsius.
    step_unit = interval_unit(unit)
    magnitude = value.to(unit).value
    minimum = allowed.minimum.to(unit).value
    maximum = allowed.maximum.to(unit).value
    issues: list[ValidationIssue] = []
    if not minimum <= magnitude <= maximum:
        issues.append(
            _error(
                code,
                path,
                rule,
                f"{magnitude} is outside [{minimum}, {maximum}] {unit}",
                value=str(magnitude),
                minimum=str(minimum),
                maximum=str(maximum),
            )
        )
    elif allowed.resolution is not None:
        step = allowed.resolution.to(step_unit).value
        if (magnitude - minimum) % step != 0:
            issues.append(
                _error(
                    "setpoint_off_resolution",
                    path,
                    rule,
                    f"{magnitude} is not a multiple of {step} from {minimum}",
                )
            )
    return issues


def _error(code: str, path: str, rule: str, message: str, **details: Any) -> ValidationIssue:
    return ValidationIssue(
        code=code,
        severity=IssueSeverity.ERROR,
        path=path,
        message=message,
        rule_name=rule,
        rule_version=RULE_VERSION,
        details=details,
    )
