"""Generate concise, hashable operator worklists from validated protocols."""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid5

from pydantic import Field, field_validator, model_validator

from autonomous_lab.domain.base import DomainModel
from autonomous_lab.domain.identifiers import require_utc, utc_now
from autonomous_lab.domain.protocols import (
    ControlDefinition,
    DispenseCommand,
    ExecutableProtocol,
    IncubateCommand,
    MaterialReference,
    MeasureAbsorbanceCommand,
    MixCommand,
    ProtocolStep,
    VesselDefinition,
)
from autonomous_lab.domain.quantities import QuantityValue
from autonomous_lab.human.models import ExecutionMode
from autonomous_lab.provenance.hashing import canonical_sha256, sha256_text

_WORKLIST_NAMESPACE = UUID("03444f98-52f6-4a76-a2e8-cb7c5aeb1a1a")


class MaterialRequirement(DomainModel):
    material_id: str
    material_name: str
    lot_id: str | None = None
    required_amount: QuantityValue
    properties: dict[str, QuantityValue] = Field(default_factory=dict)


class WellAssignment(DomainModel):
    vessel_id: str
    location: str
    role: str
    material_id: str
    replicate: int = Field(ge=1)


class PreparationRow(DomainModel):
    vessel_id: str
    location: str
    role: str
    sample_material_id: str
    reagent_volume: QuantityValue
    sample_volume: QuantityValue
    total_volume: QuantityValue
    reagent_concentration: QuantityValue | None = None
    sample_nominal_level: QuantityValue | None = None

    @model_validator(mode="after")
    def _total_is_correct(self) -> PreparationRow:
        reagent = self.reagent_volume.to(self.total_volume.unit)
        sample = self.sample_volume.to(self.total_volume.unit)
        if reagent.value + sample.value != self.total_volume.value:
            raise ValueError("total volume must equal reagent plus sample volume")
        return self


class WorklistGroup(DomainModel):
    group_id: str
    sequence: int = Field(ge=1)
    title: str
    step_ids: tuple[str, ...]
    instruction: str
    expected_next_state: str
    safety_note: str | None = None

    @field_validator("step_ids")
    @classmethod
    def _nonempty_steps(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("worklist group must contain a step")
        return value


class MeasurementSheetRow(DomainModel):
    vessel_id: str
    location: str
    role: str
    measurement_name: str
    wavelength: QuantityValue
    value: None = None
    unit: str = "absorbance_unit"
    status: Literal["pending"] = "pending"
    note: str = ""


class OperatorWorklist(DomainModel):
    schema_version: Literal["1"] = "1"
    worklist_id: UUID
    campaign_id: UUID
    campaign_name: str
    experiment_id: UUID
    protocol_id: UUID
    protocol_hash: str
    generated_at: datetime
    operator_id: str | None = None
    execution_mode: Literal[ExecutionMode.HUMAN_MANUAL, ExecutionMode.HUMAN_SENSOR]
    compiler_name: str
    compiler_version: str
    requested_steps: tuple[ProtocolStep, ...]
    materials: tuple[MaterialRequirement, ...]
    wells: tuple[WellAssignment, ...]
    preparation: tuple[PreparationRow, ...]
    procedure: tuple[WorklistGroup, ...]
    measurement_sheet: tuple[MeasurementSheetRow, ...]
    content_hash: str

    @field_validator("generated_at")
    @classmethod
    def _utc_generated_at(cls, value: datetime) -> datetime:
        return require_utc(value)

    @model_validator(mode="after")
    def _complete_protocol_coverage(self) -> OperatorWorklist:
        grouped = [step_id for group in self.procedure for step_id in group.step_ids]
        if len(grouped) != len(set(grouped)):
            raise ValueError("a protocol step cannot occur in more than one worklist group")
        if len(self.measurement_sheet) != len(self.wells):
            raise ValueError("measurement sheet must have one row per well")
        requested = [step.step_id for step in self.requested_steps]
        if grouped != requested and set(grouped) != set(requested):
            raise ValueError("worklist groups must cover every requested protocol step exactly")
        return self


class WorklistArtifact(DomainModel):
    artifact_type: Literal["markdown", "preparation_csv", "measurements_csv"]
    file_name: str
    sha256: str
    byte_count: int = Field(ge=0)


class WorklistExport(DomainModel):
    worklist_id: UUID
    directory: str
    artifacts: tuple[WorklistArtifact, ...]


class WorklistGenerator:
    """Translate an exact validation receipt into grouped operator instructions."""

    def generate(
        self,
        executable: ExecutableProtocol,
        *,
        campaign_id: UUID,
        campaign_name: str,
        execution_mode: Literal[ExecutionMode.HUMAN_MANUAL, ExecutionMode.HUMAN_SENSOR],
        operator_id: str | None = None,
        generated_at: datetime | None = None,
    ) -> OperatorWorklist:
        protocol = executable.protocol
        timestamp = generated_at or utc_now()
        worklist_id = uuid5(
            _WORKLIST_NAMESPACE,
            f"{campaign_id}:{protocol.experiment_id}:{executable.protocol_hash}",
        )
        preparation = self._preparation(protocol.steps, protocol.vessels, protocol.materials)
        roles = _well_roles(protocol.controls)
        wells = tuple(
            WellAssignment(
                vessel_id=row.vessel_id,
                location=row.location,
                role=row.role,
                material_id=row.sample_material_id,
                replicate=_replicate_number(row.vessel_id),
            )
            for row in preparation
        )
        materials = self._materials(protocol.materials, protocol.steps)
        groups = self._groups(protocol.steps)
        measurement_sheet = tuple(
            MeasurementSheetRow(
                vessel_id=vessel.vessel_id,
                location=vessel.location or vessel.vessel_id,
                role=roles.get(vessel.vessel_id, "sample"),
                measurement_name=protocol.measurement_plan.measurement_name,
                wavelength=protocol.measurement_plan.wavelength,
            )
            for vessel in protocol.vessels
        )
        unhashed = OperatorWorklist(
            worklist_id=worklist_id,
            campaign_id=campaign_id,
            campaign_name=campaign_name,
            experiment_id=protocol.experiment_id,
            protocol_id=protocol.protocol_id,
            protocol_hash=executable.protocol_hash,
            generated_at=timestamp,
            operator_id=operator_id,
            execution_mode=execution_mode,
            compiler_name=protocol.compiler_name,
            compiler_version=protocol.compiler_version,
            requested_steps=protocol.steps,
            materials=materials,
            wells=wells,
            preparation=preparation,
            procedure=groups,
            measurement_sheet=measurement_sheet,
            content_hash="0" * 64,
        )
        return unhashed.model_copy(
            update={"content_hash": canonical_sha256(unhashed, exclude={"content_hash"})}
        )

    def _materials(
        self,
        materials: tuple[MaterialReference, ...],
        steps: tuple[ProtocolStep, ...],
    ) -> tuple[MaterialRequirement, ...]:
        material_lookup = {item.material_id: item for item in materials}
        totals: dict[str, QuantityValue] = {}
        for step in steps:
            command = step.command
            if not isinstance(command, DispenseCommand):
                continue
            amount = command.volume.to("microliter")
            previous = totals.get(command.material_id)
            total = amount.value + (previous.value if previous is not None else Decimal("0"))
            totals[command.material_id] = QuantityValue(value=total, unit="microliter")
        return tuple(
            MaterialRequirement(
                material_id=material_id,
                material_name=material_lookup[material_id].name,
                lot_id=material_lookup[material_id].lot_id,
                required_amount=amount,
                properties=material_lookup[material_id].properties,
            )
            for material_id, amount in sorted(totals.items())
        )

    def _preparation(
        self,
        steps: tuple[ProtocolStep, ...],
        vessels: tuple[VesselDefinition, ...],
        materials: tuple[MaterialReference, ...],
    ) -> tuple[PreparationRow, ...]:
        roles: dict[str, str] = {}
        sample_by_vessel: dict[str, tuple[str, QuantityValue]] = {}
        reagent_by_vessel: dict[str, QuantityValue] = {}
        for step in steps:
            command = step.command
            if not isinstance(command, DispenseCommand):
                continue
            if command.material_id == "reagent":
                reagent_by_vessel[command.destination_vessel_id] = command.volume
            else:
                sample_by_vessel[command.destination_vessel_id] = (
                    command.material_id,
                    command.volume,
                )
                roles[command.destination_vessel_id] = _humanize(command.material_id)
        reagent = next(item for item in materials if item.material_id == "reagent")
        concentration = reagent.properties.get("concentration")
        material_lookup = {item.material_id: item for item in materials}
        rows: list[PreparationRow] = []
        for vessel in vessels:
            vessel_id = vessel.vessel_id
            location = vessel.location or vessel_id
            reagent_volume = reagent_by_vessel[vessel_id].to("microliter")
            sample_id, sample_volume = sample_by_vessel[vessel_id]
            sample_volume = sample_volume.to("microliter")
            rows.append(
                PreparationRow(
                    vessel_id=vessel_id,
                    location=location,
                    role=roles[vessel_id],
                    sample_material_id=sample_id,
                    reagent_volume=reagent_volume,
                    sample_volume=sample_volume,
                    total_volume=QuantityValue(
                        value=reagent_volume.value + sample_volume.value,
                        unit="microliter",
                    ),
                    reagent_concentration=concentration,
                    sample_nominal_level=material_lookup[sample_id].properties.get("nominal_level"),
                )
            )
        return tuple(rows)

    def _groups(self, steps: tuple[ProtocolStep, ...]) -> tuple[WorklistGroup, ...]:
        buckets: dict[str, list[ProtocolStep]] = defaultdict(list)
        for step in steps:
            command = step.command
            if isinstance(command, DispenseCommand):
                key = "dispense-reagent" if command.material_id == "reagent" else "dispense-samples"
            elif isinstance(command, MixCommand):
                key = "mix"
            elif isinstance(command, IncubateCommand):
                key = "incubate"
            elif isinstance(command, MeasureAbsorbanceCommand):
                key = "measure"
            else:
                key = command.command_type.value
            buckets[key].append(step)

        reagent_command = _first_command(buckets, "dispense-reagent", DispenseCommand)
        sample_command = _first_command(buckets, "dispense-samples", DispenseCommand)
        mix_command = _first_command(buckets, "mix", MixCommand)
        incubation_command = _first_command(buckets, "incubate", IncubateCommand)
        measurement_command = _first_command(buckets, "measure", MeasureAbsorbanceCommand)
        specifications = (
            (
                "dispense-reagent",
                "Dispense reagent",
                f"Dispense {_display_quantity(reagent_command.volume)} reagent into each "
                "well listed in the preparation table.",
                "All wells contain reagent.",
                "Use a fresh tip or validated contamination-control technique.",
            ),
            (
                "dispense-samples",
                "Add blanks, standards, and controls",
                f"Dispense {_display_quantity(sample_command.volume)} of each source material "
                "into its assigned well according to the preparation table.",
                "Every well has its complete nominal preparation volume.",
                "Verify source labels before dispensing; record spills or "
                "substitutions as deviations.",
            ),
            (
                "mix",
                "Mix prepared wells",
                f"Mix each prepared well for {mix_command.cycles} cycles using a "
                f"{_display_quantity(mix_command.volume)} stroke.",
                "All preparations are homogeneous.",
                "Avoid bubbles and cross-contamination.",
            ),
            (
                "incubate",
                "Incubate the batch",
                f"Incubate all {len(incubation_command.vessel_ids)} prepared wells for "
                f"{_display_quantity(incubation_command.duration)} at "
                f"{_display_quantity(incubation_command.temperature)}. Record actual elapsed "
                "time and temperature if they differ.",
                "Incubation is complete and the plate is ready to read.",
                None,
            ),
            (
                "measure",
                "Measure absorbance",
                "Acquire one reading for every measurement-sheet row at the specified "
                f"wavelength of {_display_quantity(measurement_command.wavelength)}. "
                "Do not invent unavailable readings.",
                "The expected measurement plan is ready for reconciliation.",
                "Preserve raw instrument output when a sensor is used.",
            ),
        )
        groups: list[WorklistGroup] = []
        for key, title, instruction, expected, safety in specifications:
            if key not in buckets:
                continue
            groups.append(
                WorklistGroup(
                    group_id=key,
                    sequence=len(groups) + 1,
                    title=title,
                    step_ids=tuple(step.step_id for step in buckets[key]),
                    instruction=instruction,
                    expected_next_state=expected,
                    safety_note=safety,
                )
            )
        handled = {step_id for group in groups for step_id in group.step_ids}
        for step in steps:
            if step.step_id in handled:
                continue
            groups.append(
                WorklistGroup(
                    group_id=f"step-{len(groups) + 1}",
                    sequence=len(groups) + 1,
                    title=_humanize(step.command.command_type.value),
                    step_ids=(step.step_id,),
                    instruction=f"Perform protocol step {step.step_id}.",
                    expected_next_state="The step postconditions have been checked.",
                )
            )
        return tuple(groups)


def render_markdown(worklist: OperatorWorklist) -> str:
    """Render a printable, copyable human worklist with explicit units."""

    lines = [
        f"# AutoLab operator worklist — {worklist.campaign_name}",
        "",
        f"- Campaign ID: `{worklist.campaign_id}`",
        f"- Experiment ID: `{worklist.experiment_id}`",
        f"- Protocol hash: `{worklist.protocol_hash}`",
        f"- Generated: {worklist.generated_at.isoformat()}",
        f"- Operator: {worklist.operator_id or '________________'}",
        f"- Execution mode: `{worklist.execution_mode.value}`",
        f"- Compiler: `{worklist.compiler_name} {worklist.compiler_version}`",
        f"- Worklist content hash: `{worklist.content_hash}`",
        "",
        "## Materials",
        "",
        "| Material | Nominal level / property | Required amount | Lot |",
        "|---|---:|---:|---|",
    ]
    lines.extend(
        f"| {_humanize(item.material_name)} (`{item.material_id}`) | "
        f"{_material_properties(item)} | "
        f"{_display_quantity(item.required_amount, human_volume=True)} | "
        f"{item.lot_id or '—'} |"
        for item in worklist.materials
    )
    lines.extend(
        [
            "",
            "Amounts are exact protocol totals and do not include dead-volume contingency.",
            "",
            "## Plate layout",
            "",
            "| Well | Sample / role | Internal vessel ID |",
            "|---|---|---|",
        ]
    )
    lines.extend(
        f"| {row.location} | {_humanize(row.role)} "
        f"(replicate {row.replicate}) | `{row.vessel_id}` |"
        for row in worklist.wells
    )
    lines.extend(
        [
            "",
            "## Preparation table",
            "",
            "| Well | Source | Nominal level | Reagent | Sample | Total | Reagent concentration |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    lines.extend(
        "| "
        + " | ".join(
            (
                row.location,
                f"{_humanize(row.role)} (`{row.sample_material_id}`)",
                _display_quantity(row.sample_nominal_level),
                _display_quantity(row.reagent_volume),
                _display_quantity(row.sample_volume),
                _display_quantity(row.total_volume),
                _display_quantity(row.reagent_concentration) if row.reagent_concentration else "—",
            )
        )
        + " |"
        for row in worklist.preparation
    )
    lines.extend(["", "## Procedure", ""])
    for group in worklist.procedure:
        lines.extend(
            [
                f"### {group.sequence}. {group.title}",
                "",
                group.instruction,
                "",
                f"Covers {len(group.step_ids)} protocol step(s). "
                f"Expected next state: {group.expected_next_state}",
            ]
        )
        if group.safety_note:
            lines.append(f"Operator note: {group.safety_note}")
        lines.extend(
            [
                "",
                "Completion: [ ] confirmed  [ ] deviation recorded  [ ] skipped  "
                "[ ] failed  [ ] abort",
                "",
                "Actual quantity/time (if different): ________________________________",
                "",
                "Deviation category and notes: _______________________________________",
                "",
            ]
        )
    wavelength = worklist.measurement_sheet[0].wavelength if worklist.measurement_sheet else None
    lines.extend(
        [
            "## Measurement sheet",
            "",
            f"Target wavelength: {_display_quantity(wavelength) if wavelength else '—'}",
            "",
            "| Well | Role | Measurement | Value | Unit | Status / note |",
            "|---|---|---|---:|---|---|",
        ]
    )
    lines.extend(
        f"| {row.location} | {_humanize(row.role)} | {row.measurement_name} |  | "
        f"{_display_unit(row.unit)} |  |"
        for row in worklist.measurement_sheet
    )
    lines.extend(
        [
            "",
            "Missing or unavailable measurements must be marked explicitly; never estimate them.",
            "",
        ]
    )
    return "\n".join(lines)


def render_preparation_csv(worklist: OperatorWorklist) -> str:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(
        (
            "well",
            "vessel_id",
            "role",
            "sample_material_id",
            "sample_nominal_level",
            "sample_nominal_level_unit",
            "reagent_volume",
            "reagent_unit",
            "sample_volume",
            "sample_unit",
            "total_volume",
            "total_unit",
            "reagent_concentration",
            "concentration_unit",
        )
    )
    for row in worklist.preparation:
        writer.writerow(
            (
                row.location,
                row.vessel_id,
                row.role,
                row.sample_material_id,
                (_decimal_text(row.sample_nominal_level.value) if row.sample_nominal_level else ""),
                row.sample_nominal_level.unit if row.sample_nominal_level else "",
                _decimal_text(row.reagent_volume.value),
                row.reagent_volume.unit,
                _decimal_text(row.sample_volume.value),
                row.sample_volume.unit,
                _decimal_text(row.total_volume.value),
                row.total_volume.unit,
                _decimal_text(row.reagent_concentration.value) if row.reagent_concentration else "",
                row.reagent_concentration.unit if row.reagent_concentration else "",
            )
        )
    return stream.getvalue()


def render_measurements_csv(worklist: OperatorWorklist) -> str:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(
        (
            "sample_id",
            "measurement_type",
            "wavelength_nm",
            "value",
            "unit",
            "timestamp",
            "status",
            "notes",
        )
    )
    for row in worklist.measurement_sheet:
        writer.writerow(
            (
                row.vessel_id,
                row.measurement_name,
                _decimal_text(row.wavelength.to("nanometer").value),
                "",
                row.unit,
                "",
                "",
                "",
            )
        )
    return stream.getvalue()


def export_worklist(worklist: OperatorWorklist, directory: str | Path) -> WorklistExport:
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    stem = f"experiment-{worklist.experiment_id}"
    documents: tuple[
        tuple[Literal["markdown", "preparation_csv", "measurements_csv"], str, str], ...
    ] = (
        ("markdown", f"{stem}-worklist.md", render_markdown(worklist)),
        ("preparation_csv", f"{stem}-preparation.csv", render_preparation_csv(worklist)),
        ("measurements_csv", f"{stem}-measurements.csv", render_measurements_csv(worklist)),
    )
    artifacts: list[WorklistArtifact] = []
    for kind, file_name, content in documents:
        encoded = content.encode("utf-8")
        (target / file_name).write_bytes(encoded)
        artifacts.append(
            WorklistArtifact(
                artifact_type=kind,
                file_name=file_name,
                sha256=sha256_text(content),
                byte_count=len(encoded),
            )
        )
    return WorklistExport(
        worklist_id=worklist.worklist_id,
        directory=str(target),
        artifacts=tuple(artifacts),
    )


def _well_roles(controls: tuple[ControlDefinition, ...]) -> dict[str, str]:
    result: dict[str, str] = {}
    for control in controls:
        role = control.control_type.value
        for vessel_id in control.vessel_ids:
            result[vessel_id] = role
    return result


def _first_command[
    CommandT: (DispenseCommand, MixCommand, IncubateCommand, MeasureAbsorbanceCommand)
](
    buckets: dict[str, list[ProtocolStep]],
    key: str,
    command_type: type[CommandT],
) -> CommandT:
    steps = buckets.get(key)
    if not steps or not isinstance(steps[0].command, command_type):
        raise ValueError(f"V0.5 assay worklist requires a {key!r} operation group")
    return steps[0].command


def _replicate_number(vessel_id: str) -> int:
    suffix = vessel_id.rsplit("-", maxsplit=1)[-1]
    return int(suffix) if suffix.isdigit() else 1


def _humanize(value: str) -> str:
    return value.replace("_", " ").replace("-", " ").strip().capitalize()


def _decimal_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _display_quantity(quantity: QuantityValue | None, *, human_volume: bool = False) -> str:
    if quantity is None:
        return "—"
    shown = quantity
    if human_volume and quantity.is_compatible_with("milliliter"):
        in_microliter = quantity.to("microliter")
        if in_microliter.value >= 1000:
            shown = quantity.to("milliliter")
    unit = _display_unit(shown.unit)
    return f"{_decimal_text(shown.value)} {unit}"


def _display_unit(unit: str) -> str:
    return {
        "microliter": "µL",
        "milliliter": "mL",
        "milligram / milliliter": "mg/mL",
        "minute": "min",
        "degree_Celsius": "°C",
        "nanometer": "nm",
        "absorbance_unit": "AU",
        "dimensionless": "(dimensionless)",
    }.get(unit, unit)


def _material_properties(material: MaterialRequirement) -> str:
    if not material.properties:
        return "—"
    return "; ".join(
        f"{_humanize(name)}: {_display_quantity(quantity)}"
        for name, quantity in sorted(material.properties.items())
    )
