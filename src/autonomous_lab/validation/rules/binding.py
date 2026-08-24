"""Bind exact V0 assay commands and materials back to scientific parameters."""

from __future__ import annotations

from collections import Counter, defaultdict

from autonomous_lab.domain.protocols import (
    AspirateCommand,
    DispenseCommand,
    IncubateCommand,
    MeasureAbsorbanceCommand,
    MixCommand,
    TransferCommand,
    ValidationIssue,
)
from autonomous_lab.domain.quantities import QuantityValue
from autonomous_lab.validation.rules.base import ValidationContext, error

RULE = "assay_recipe_binding"


def validate(context: ValidationContext) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    protocol = context.protocol
    values = protocol.parameter_values
    required = {
        "reagent_concentration",
        "reagent_volume",
        "sample_volume",
        "incubation_time",
        "temperature",
    }
    if not required.issubset(values):
        return ()  # Parameter rules emit the actionable missing-field issues.

    if protocol.compiler_name != "v0_colorimetric_assay" or protocol.compiler_version != "1.1.0":
        issues.append(
            error(
                "compiler.untrusted",
                "compiler_name",
                "V0 validation only trusts the registered assay compiler version",
                RULE,
                details={
                    "compiler_name": protocol.compiler_name,
                    "compiler_version": protocol.compiler_version,
                },
            )
        )

    materials = {material.material_id: material for material in protocol.materials}
    reagent_materials = [
        material for material in protocol.materials if material.name == "reagent"
    ]
    if len(reagent_materials) != 1:
        issues.append(
            error(
                "recipe.reagent_material_count",
                "materials",
                "protocol must define exactly one reagent material",
                RULE,
                details={"actual": len(reagent_materials)},
            )
        )
        reagent_id = None
    else:
        reagent_id = reagent_materials[0].material_id
        concentration = reagent_materials[0].properties.get("concentration")
        if concentration is None or not _same_quantity(
            concentration,
            values["reagent_concentration"],
        ):
            issues.append(
                error(
                    "recipe.reagent_concentration_mismatch",
                    "materials",
                    "reagent material concentration differs from the proposal",
                    RULE,
                )
            )

    dispenses: dict[str, list[DispenseCommand]] = defaultdict(list)
    mixes: Counter[str] = Counter()
    used_materials: set[str] = set()
    incubations: list[IncubateCommand] = []
    measurements: list[MeasureAbsorbanceCommand] = []
    for step in protocol.steps:
        command = step.command
        if isinstance(command, DispenseCommand):
            dispenses[command.destination_vessel_id].append(command)
            used_materials.add(command.material_id)
        elif isinstance(command, MixCommand):
            mixes[command.vessel_id] += 1
        elif isinstance(command, IncubateCommand):
            incubations.append(command)
        elif isinstance(command, (AspirateCommand, TransferCommand)):
            issues.append(
                error(
                    "recipe.incompatible_liquid_operation",
                    "steps",
                    "V0 assay protocols cannot aspirate or transfer between assay wells",
                    RULE,
                    details={"command_type": command.command_type.value},
                )
            )
        else:
            measurements.append(command)

    reagent_volume = values["reagent_volume"]
    sample_volume = values["sample_volume"]
    for vessel in protocol.vessels:
        commands = dispenses[vessel.vessel_id]
        reagent_commands = [command for command in commands if command.material_id == reagent_id]
        sample_commands = [command for command in commands if command.material_id != reagent_id]
        if len(commands) != 2 or len(reagent_commands) != 1 or len(sample_commands) != 1:
            issues.append(
                error(
                    "recipe.dispense_layout_mismatch",
                    "steps",
                    f"vessel {vessel.vessel_id!r} must receive one reagent and one sample dispense",
                    RULE,
                    details={"vessel_id": vessel.vessel_id},
                )
            )
        else:
            if not _same_quantity(reagent_commands[0].volume, reagent_volume):
                issues.append(
                    error(
                        "recipe.reagent_volume_mismatch",
                        "steps",
                        f"reagent volume for vessel {vessel.vessel_id!r} differs from proposal",
                        RULE,
                    )
                )
            if not _same_quantity(sample_commands[0].volume, sample_volume):
                issues.append(
                    error(
                        "recipe.sample_volume_mismatch",
                        "steps",
                        f"sample volume for vessel {vessel.vessel_id!r} differs from proposal",
                        RULE,
                    )
                )
            sample_material = materials.get(sample_commands[0].material_id)
            if sample_material is not None and sample_material.name == "reagent":
                issues.append(
                    error(
                        "recipe.missing_assay_source",
                        "steps",
                        f"vessel {vessel.vessel_id!r} has no non-reagent assay source",
                        RULE,
                    )
                )
        if mixes[vessel.vessel_id] != 1:
            issues.append(
                error(
                    "recipe.mix_count_mismatch",
                    "steps",
                    f"vessel {vessel.vessel_id!r} must be mixed exactly once",
                    RULE,
                    details={"actual": mixes[vessel.vessel_id]},
                )
            )

    unused_materials = sorted(set(materials) - used_materials)
    if unused_materials:
        issues.append(
            error(
                "recipe.unused_material",
                "materials",
                "all declared materials must be used by the protocol",
                RULE,
                details={"material_ids": unused_materials},
            )
        )

    all_vessels = tuple(vessel.vessel_id for vessel in protocol.vessels)
    if len(incubations) != 1:
        issues.append(
            error(
                "recipe.incubation_count_mismatch",
                "steps",
                "V0 assay protocol must contain exactly one batch incubation",
                RULE,
                details={"actual": len(incubations)},
            )
        )
    else:
        incubation = incubations[0]
        if incubation.vessel_ids != all_vessels:
            issues.append(
                error(
                    "recipe.incubation_vessels_mismatch",
                    "steps",
                    "incubation must cover every assay vessel in layout order",
                    RULE,
                )
            )
        if not _same_quantity(incubation.duration, values["incubation_time"]):
            issues.append(
                error(
                    "recipe.incubation_time_mismatch",
                    "steps",
                    "incubation duration differs from the proposal",
                    RULE,
                )
            )
        if not _same_quantity(incubation.temperature, values["temperature"]):
            issues.append(
                error(
                    "recipe.temperature_mismatch",
                    "steps",
                    "incubation temperature differs from the proposal",
                    RULE,
                )
            )

    expected_wavelength = context.campaign.assay_design.wavelength
    if not _same_quantity(protocol.measurement_plan.wavelength, expected_wavelength):
        issues.append(
            error(
                "recipe.wavelength_mismatch",
                "measurement_plan.wavelength",
                "measurement wavelength differs from the campaign assay design",
                RULE,
            )
        )
    for measurement in measurements:
        if not _same_quantity(measurement.wavelength, expected_wavelength):
            issues.append(
                error(
                    "recipe.wavelength_mismatch",
                    "steps",
                    "measurement command wavelength differs from the campaign assay design",
                    RULE,
                )
            )
    return tuple(issues)


def _same_quantity(left: QuantityValue, right: QuantityValue) -> bool:
    try:
        return left.to(right.unit).value == right.value
    except ValueError:
        return False
