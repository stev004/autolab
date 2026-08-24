"""References, trusted allow-lists, and measurement-plan consistency."""

from __future__ import annotations

from autonomous_lab.domain.protocols import (
    AspirateCommand,
    DispenseCommand,
    MeasureAbsorbanceCommand,
    MixCommand,
    TransferCommand,
    ValidationIssue,
)
from autonomous_lab.validation.rules.base import ValidationContext, error

RULE = "protocol_structure"


def validate(context: ValidationContext) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    protocol = context.protocol
    material_ids = [material.material_id for material in protocol.materials]
    known_materials = set(material_ids)
    vessel_ids = [vessel.vessel_id for vessel in protocol.vessels]
    known_vessels = set(vessel_ids)

    if len(material_ids) != len(known_materials):
        issues.append(
            error(
                "material.duplicate_id",
                "materials",
                "material IDs must be unique",
                RULE,
            )
        )
    for index, material in enumerate(protocol.materials):
        if material.name not in context.policy.allowed_materials:
            issues.append(
                error(
                    "material.not_permitted",
                    f"materials.{index}.name",
                    f"material {material.name!r} is not permitted by trusted policy",
                    RULE,
                    details={"material_id": material.material_id},
                )
            )
    for index, vessel in enumerate(protocol.vessels):
        if vessel.vessel_type not in context.policy.supported_vessel_types:
            issues.append(
                error(
                    "vessel.type_not_permitted",
                    f"vessels.{index}.vessel_type",
                    f"vessel type {vessel.vessel_type!r} is not permitted",
                    RULE,
                )
            )

    for index, step in enumerate(protocol.steps):
        command = step.command
        path = f"steps.{index}.command"
        if command.command_type not in context.policy.allowed_command_types:
            issues.append(
                error(
                    "command.not_permitted",
                    f"{path}.command_type",
                    f"command {command.command_type.value!r} is not permitted",
                    RULE,
                )
            )
        if not step.required_capability.strip():
            issues.append(
                error(
                    "command.missing_required_capability",
                    f"steps.{index}.required_capability",
                    "every protocol step must name a required capability",
                    RULE,
                )
            )
        if isinstance(command, DispenseCommand):
            if command.material_id not in known_materials:
                issues.append(
                    error(
                        "command.unknown_material",
                        f"{path}.material_id",
                        f"dispense references unknown material {command.material_id!r}",
                        RULE,
                    )
                )
            _unknown_vessel(issues, command.destination_vessel_id, known_vessels, path, RULE)
        elif isinstance(command, AspirateCommand):
            _unknown_vessel(issues, command.source_vessel_id, known_vessels, path, RULE)
        elif isinstance(command, TransferCommand):
            _unknown_vessel(issues, command.source_vessel_id, known_vessels, path, RULE)
            _unknown_vessel(issues, command.destination_vessel_id, known_vessels, path, RULE)
            if command.source_vessel_id == command.destination_vessel_id:
                issues.append(
                    error(
                        "command.nonsensical_self_transfer",
                        path,
                        "a transfer source and destination cannot be the same vessel",
                        RULE,
                    )
                )
        elif isinstance(command, MixCommand):
            _unknown_vessel(issues, command.vessel_id, known_vessels, path, RULE)
        else:
            if not command.vessel_ids:
                issues.append(
                    error(
                        "command.empty_vessel_set",
                        f"{path}.vessel_ids",
                        "batch commands must target at least one vessel",
                        RULE,
                    )
                )
            if len(command.vessel_ids) != len(set(command.vessel_ids)):
                issues.append(
                    error(
                        "command.duplicate_vessel",
                        f"{path}.vessel_ids",
                        "batch command vessel IDs must be unique",
                        RULE,
                    )
                )
            for vessel_id in command.vessel_ids:
                _unknown_vessel(issues, vessel_id, known_vessels, path, RULE)

    plan_ids = protocol.measurement_plan.vessel_ids
    if not plan_ids:
        issues.append(
            error(
                "measurement.empty_plan",
                "measurement_plan.vessel_ids",
                "measurement plan must contain at least one vessel",
                RULE,
            )
        )
    if len(plan_ids) != len(set(plan_ids)):
        issues.append(
            error(
                "measurement.duplicate_vessel",
                "measurement_plan.vessel_ids",
                "measurement plan vessel IDs must be unique",
                RULE,
            )
        )
    for vessel_id in plan_ids:
        _unknown_vessel(issues, vessel_id, known_vessels, "measurement_plan", RULE)
    if set(plan_ids) != known_vessels:
        issues.append(
            error(
                "measurement.incomplete_plan",
                "measurement_plan.vessel_ids",
                "V0 measurement plan must cover every protocol vessel exactly once",
                RULE,
                details={
                    "missing": sorted(known_vessels - set(plan_ids)),
                    "extra": sorted(set(plan_ids) - known_vessels),
                },
            )
        )

    measure_steps = [
        step.command
        for step in protocol.steps
        if isinstance(step.command, MeasureAbsorbanceCommand)
    ]
    if len(measure_steps) != 1:
        issues.append(
            error(
                "measurement.measure_step_count",
                "steps",
                "V0 protocol must contain exactly one absorbance measurement step",
                RULE,
                details={"actual": len(measure_steps)},
            )
        )
    elif (
        measure_steps[0].vessel_ids != plan_ids
        or measure_steps[0].wavelength.to("nanometer")
        != protocol.measurement_plan.wavelength.to("nanometer")
    ):
        issues.append(
            error(
                "measurement.command_plan_mismatch",
                "measurement_plan",
                "measurement command does not exactly match the measurement plan",
                RULE,
            )
        )
    return tuple(issues)


def _unknown_vessel(
    issues: list[ValidationIssue],
    vessel_id: str,
    known_vessels: set[str],
    path: str,
    rule: str,
) -> None:
    if vessel_id not in known_vessels:
        issues.append(
            error(
                "command.unknown_vessel",
                path,
                f"command references unknown vessel {vessel_id!r}",
                rule,
            )
        )
