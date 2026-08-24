"""Control presence, calibration levels, and replicate checks."""

from __future__ import annotations

from collections import Counter

from autonomous_lab.domain.enums import ControlType
from autonomous_lab.domain.protocols import ValidationIssue
from autonomous_lab.validation.rules.base import ValidationContext, error

RULE = "assay_controls"


def validate(context: ValidationContext) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    controls = context.protocol.controls
    by_type = Counter(control.control_type for control in controls)
    assigned_vessels: list[str] = []
    known_vessels = {vessel.vessel_id for vessel in context.protocol.vessels}

    for required in sorted(context.policy.required_control_types, key=str):
        if by_type[required] == 0:
            issues.append(
                error(
                    "control.required_type_missing",
                    "controls",
                    f"required control type {required.value!r} is missing",
                    RULE,
                    details={"control_type": required.value},
                )
            )

    for index, control in enumerate(controls):
        if not control.vessel_ids:
            issues.append(
                error(
                    "control.empty_replicates",
                    f"controls.{index}.vessel_ids",
                    "a control must reference at least one replicate vessel",
                    RULE,
                )
            )
        if len(control.vessel_ids) != len(set(control.vessel_ids)):
            issues.append(
                error(
                    "control.duplicate_replicate",
                    f"controls.{index}.vessel_ids",
                    "a control cannot reference the same vessel more than once",
                    RULE,
                )
            )
        assigned_vessels.extend(control.vessel_ids)
        unknown = sorted(set(control.vessel_ids) - known_vessels)
        if unknown:
            issues.append(
                error(
                    "control.unknown_vessel",
                    f"controls.{index}.vessel_ids",
                    "control references unknown vessels",
                    RULE,
                    details={"vessel_ids": unknown},
                )
            )
        if (
            control.control_type
            in {
                ControlType.STANDARD,
                ControlType.POSITIVE,
                ControlType.CHECK_STANDARD,
            }
            and control.nominal_level is None
        ):
            issues.append(
                error(
                    "control.nominal_level_missing",
                    f"controls.{index}.nominal_level",
                    "standards and positive controls require a nominal level",
                    RULE,
                )
            )
        if control.nominal_level is not None and control.nominal_level < 0:
            issues.append(
                error(
                    "control.negative_nominal_level",
                    f"controls.{index}.nominal_level",
                    "control nominal level cannot be negative",
                    RULE,
                )
            )

    repeated_assignments = sorted(
        vessel_id for vessel_id, count in Counter(assigned_vessels).items() if count > 1
    )
    if repeated_assignments:
        issues.append(
            error(
                "control.vessel_assigned_multiple_roles",
                "controls",
                "a vessel cannot be assigned to multiple control definitions",
                RULE,
                details={"vessel_ids": repeated_assignments},
            )
        )

    blank_replicates = sum(
        len(control.vessel_ids) for control in controls if control.control_type is ControlType.BLANK
    )
    if blank_replicates < context.policy.minimum_blank_replicates:
        issues.append(
            error(
                "control.insufficient_blank_replicates",
                "controls",
                "insufficient blank replicates",
                RULE,
                details={
                    "minimum": context.policy.minimum_blank_replicates,
                    "actual": blank_replicates,
                },
            )
        )

    standards = [control for control in controls if control.control_type is ControlType.STANDARD]
    levels = [control.nominal_level for control in standards if control.nominal_level is not None]
    if len(set(levels)) < context.policy.minimum_standard_levels:
        issues.append(
            error(
                "control.insufficient_standard_levels",
                "controls",
                "insufficient distinct calibration standard levels",
                RULE,
                details={
                    "minimum": context.policy.minimum_standard_levels,
                    "actual": len(set(levels)),
                },
            )
        )
    if len(levels) != len(set(levels)):
        issues.append(
            error(
                "control.duplicate_standard_level",
                "controls",
                "calibration standard levels must be unique",
                RULE,
            )
        )
    for index, standard in enumerate(standards):
        if len(standard.vessel_ids) < context.policy.minimum_standard_replicates:
            issues.append(
                error(
                    "control.insufficient_standard_replicates",
                    f"controls.standard.{index}.vessel_ids",
                    "insufficient replicates for a calibration level",
                    RULE,
                    details={
                        "minimum": context.policy.minimum_standard_replicates,
                        "actual": len(standard.vessel_ids),
                    },
                )
            )

    positive_replicates = sum(
        len(control.vessel_ids)
        for control in controls
        if control.control_type is ControlType.POSITIVE
    )
    if positive_replicates < context.policy.minimum_control_replicates:
        issues.append(
            error(
                "control.insufficient_positive_replicates",
                "controls",
                "insufficient positive-control replicates",
                RULE,
                details={
                    "minimum": context.policy.minimum_control_replicates,
                    "actual": positive_replicates,
                },
            )
        )
    design = context.campaign.assay_design
    if blank_replicates != design.blank_replicates:
        issues.append(
            error(
                "control.blank_layout_mismatch",
                "controls",
                "blank replicate count differs from the campaign assay design",
                RULE,
                details={"expected": design.blank_replicates, "actual": blank_replicates},
            )
        )
    actual_standard_layout = {
        standard.nominal_level: len(standard.vessel_ids)
        for standard in standards
        if standard.nominal_level is not None
    }
    expected_standard_layout = {
        level: design.standard_replicates for level in design.standard_levels
    }
    if actual_standard_layout != expected_standard_layout:
        issues.append(
            error(
                "control.standard_layout_mismatch",
                "controls",
                "calibration levels or replicate counts differ from the campaign assay design",
                RULE,
            )
        )
    if positive_replicates != design.control_replicates:
        issues.append(
            error(
                "control.positive_layout_mismatch",
                "controls",
                "positive-control replicate count differs from the campaign assay design",
                RULE,
                details={
                    "expected": design.control_replicates,
                    "actual": positive_replicates,
                },
            )
        )
    positive_levels = {
        control.nominal_level
        for control in controls
        if control.control_type is ControlType.POSITIVE
    }
    if positive_levels != {design.control_level}:
        issues.append(
            error(
                "control.positive_level_mismatch",
                "controls",
                "positive-control level differs from the campaign assay design",
                RULE,
            )
        )
    check_controls = [
        control for control in controls if control.control_type is ControlType.CHECK_STANDARD
    ]
    check_layout_matches = (
        len(check_controls) == 1
        and check_controls[0].nominal_level == design.check_standard_level
        and len(check_controls[0].vessel_ids) == design.check_standard_replicates
    )
    if not check_layout_matches:
        issues.append(
            error(
                "control.check_standard_layout_mismatch",
                "controls",
                "check-standard layout differs from the campaign assay design",
                RULE,
            )
        )
    return tuple(issues)
