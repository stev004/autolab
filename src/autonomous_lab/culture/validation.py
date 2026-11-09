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
    LIGHT_UNIT,
    TEMPERATURE_UNIT,
    CultureControllerCapabilities,
    CultureDesign,
    CulturePolicy,
    CultureProtocol,
    ExecutableCultureProtocol,
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
        if protocol.hold != expected.hold:
            issues.append(
                _error(
                    "hold_not_from_proposal",
                    "hold",
                    rule,
                    "hold conditions differ from what the proposal compiles to",
                    expected=expected.hold.model_dump(mode="json"),
                )
            )
        if protocol.measurement_plan != expected.measurement_plan:
            issues.append(
                _error(
                    "plan_not_from_design",
                    "measurement_plan",
                    rule,
                    "sampling plan differs from what the design compiles to",
                )
            )
        if protocol.parameter_values != proposal.parameter_values:
            issues.append(
                _error("parameter_snapshot_mismatch", "parameter_values", rule, "values differ")
            )
        return issues


def _policy(protocol: CultureProtocol, policy: CulturePolicy) -> list[ValidationIssue]:
    rule = "culture_policy"
    hold = protocol.hold
    issues = [
        *_range_issues(
            hold.temperature_setpoint,
            policy.temperature_setpoint_range,
            TEMPERATURE_UNIT,
            code="setpoint_outside_policy",
            path="hold.temperature_setpoint",
            rule=rule,
        ),
        *_range_issues(
            hold.light_intensity,
            policy.light_intensity_range,
            LIGHT_UNIT,
            code="light_outside_policy",
            path="hold.light_intensity",
            rule=rule,
        ),
    ]
    if seconds(hold.duration) > seconds(policy.maximum_duration):
        issues.append(
            _error("duration_exceeds_policy", "hold.duration", rule, "hold is longer than allowed")
        )
    points = protocol.measurement_plan.points
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


def _capability(
    protocol: CultureProtocol,
    policy: CulturePolicy,
    capabilities: CultureControllerCapabilities,
) -> list[ValidationIssue]:
    rule = "culture_capability"
    hold = protocol.hold
    issues: list[ValidationIssue] = []
    if hold.reactor_id not in capabilities.reactor_ids:
        issues.append(
            _error("unknown_reactor", "hold.reactor_id", rule, f"no reactor {hold.reactor_id!r}")
        )
    issues += _range_issues(
        hold.temperature_setpoint,
        capabilities.temperature_setpoint_range,
        TEMPERATURE_UNIT,
        code="setpoint_outside_device_range",
        path="hold.temperature_setpoint",
        rule=rule,
    )
    issues += _range_issues(
        hold.light_intensity,
        capabilities.light_intensity_range,
        LIGHT_UNIT,
        code="light_outside_device_range",
        path="hold.light_intensity",
        rule=rule,
    )
    if seconds(hold.duration) > seconds(capabilities.maximum_hold_duration):
        issues.append(
            _error("duration_exceeds_device", "hold.duration", rule, "device cannot hold that long")
        )
    setpoint = hold.temperature_setpoint.to(TEMPERATURE_UNIT).value
    trip = capabilities.over_temperature_trip.to(TEMPERATURE_UNIT).value
    margin = policy.minimum_trip_margin.to("delta_degree_Celsius").value
    if trip - setpoint < margin:
        issues.append(
            _error(
                "setpoint_too_close_to_trip",
                "hold.temperature_setpoint",
                rule,
                f"setpoint must stay {margin} degC below the {trip} degC protection trip",
            )
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
    step_unit = "delta_degree_Celsius" if unit == TEMPERATURE_UNIT else unit
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
                    f"{code.split('_outside')[0]}_off_resolution",
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
