"""Deterministic compiler for the V0 colorimetric assay batch."""

from __future__ import annotations

from collections.abc import Iterable
from decimal import ROUND_FLOOR, Decimal
from uuid import UUID, uuid5

from autonomous_lab.campaigns.config import AssayDesignConfig, CampaignConfig
from autonomous_lab.domain.enums import ControlType
from autonomous_lab.domain.experiments import ExperimentProposal
from autonomous_lab.domain.protocols import (
    ControlDefinition,
    DispenseCommand,
    ExperimentProtocol,
    IncubateCommand,
    MaterialReference,
    MeasureAbsorbanceCommand,
    MeasurementPlan,
    MixCommand,
    ProtocolStep,
    VesselDefinition,
)
from autonomous_lab.domain.quantities import QuantityValue
from autonomous_lab.provenance.hashing import canonical_sha256

_PROTOCOL_NAMESPACE = UUID("c419a44b-a248-4be8-8c3a-fc74ae51f2fa")


class AssayProtocolCompiler:
    """Compile one optimiser proposal into a complete calibration/control batch."""

    name = "v0_colorimetric_assay"
    version = "1.1.0"
    vessel_type = "microplate_96_well"
    mix_fraction = Decimal("0.8")
    maximum_mix_stroke_microliter = Decimal("200")
    mix_resolution_microliter = Decimal("0.1")

    def compile(
        self,
        proposal: ExperimentProposal,
        campaign: CampaignConfig,
    ) -> ExperimentProtocol:
        design = campaign.assay_design
        parameters = proposal.parameter_values
        reagent_volume = parameters["reagent_volume"].to("microliter")
        sample_volume = parameters["sample_volume"].to("microliter")
        concentration = parameters["reagent_concentration"].to("milligram / milliliter")
        incubation_time = parameters["incubation_time"].to("minute")
        temperature = parameters["temperature"].to("degree_Celsius")
        maximum_volume = design.maximum_well_volume.to("microliter")

        vessels, controls, source_by_vessel, materials = self._layout(design, maximum_volume)
        steps = self._steps(
            vessels=vessels,
            source_by_vessel=source_by_vessel,
            reagent_volume=reagent_volume,
            sample_volume=sample_volume,
            incubation_time=incubation_time,
            temperature=temperature,
            wavelength=design.wavelength,
        )
        reagent = MaterialReference(
            material_id="reagent",
            name="reagent",
            properties={"concentration": concentration},
        )
        protocol_id = uuid5(
            _PROTOCOL_NAMESPACE,
            f"{proposal.proposal_id}:{self.name}:{self.version}",
        )
        return ExperimentProtocol(
            protocol_id=protocol_id,
            experiment_id=proposal.experiment_id,
            source_proposal_id=proposal.proposal_id,
            source_proposal_hash=canonical_sha256(proposal),
            parameter_values=dict(parameters),
            compiler_name=self.name,
            compiler_version=self.version,
            materials=(reagent, *materials),
            vessels=vessels,
            controls=controls,
            steps=steps,
            measurement_plan=MeasurementPlan(
                wavelength=design.wavelength.to("nanometer"),
                vessel_ids=tuple(vessel.vessel_id for vessel in vessels),
            ),
            created_at=proposal.created_at,
        )

    def _layout(
        self,
        design: AssayDesignConfig,
        maximum_volume: QuantityValue,
    ) -> tuple[
        tuple[VesselDefinition, ...],
        tuple[ControlDefinition, ...],
        dict[str, str],
        tuple[MaterialReference, ...],
    ]:
        vessels: list[VesselDefinition] = []
        controls: list[ControlDefinition] = []
        source_by_vessel: dict[str, str] = {}
        materials: list[MaterialReference] = [
            MaterialReference(material_id="blank", name="blank"),
        ]

        blank_ids = self._add_replicates(
            vessels,
            source_by_vessel,
            prefix="blank",
            count=design.blank_replicates,
            material_id="blank",
            maximum_volume=maximum_volume,
        )
        controls.append(
            ControlDefinition(
                control_id="blank",
                control_type=ControlType.BLANK,
                vessel_ids=blank_ids,
                nominal_level=Decimal("0"),
            )
        )

        for level_index, level in enumerate(design.standard_levels, start=1):
            material_id = f"calibration-standard-{level_index:02d}"
            materials.append(
                MaterialReference(
                    material_id=material_id,
                    name="calibration_standard",
                    properties={"nominal_level": QuantityValue(value=level, unit="dimensionless")},
                )
            )
            vessel_ids = self._add_replicates(
                vessels,
                source_by_vessel,
                prefix=f"standard-{level_index:02d}",
                count=design.standard_replicates,
                material_id=material_id,
                maximum_volume=maximum_volume,
            )
            controls.append(
                ControlDefinition(
                    control_id=f"standard-{level_index:02d}",
                    control_type=ControlType.STANDARD,
                    vessel_ids=vessel_ids,
                    nominal_level=level,
                )
            )

        materials.append(
            MaterialReference(
                material_id="positive-control",
                name="positive_control",
                properties={
                    "nominal_level": QuantityValue(
                        value=design.control_level,
                        unit="dimensionless",
                    )
                },
            )
        )
        positive_ids = self._add_replicates(
            vessels,
            source_by_vessel,
            prefix="positive-control",
            count=design.control_replicates,
            material_id="positive-control",
            maximum_volume=maximum_volume,
        )
        controls.append(
            ControlDefinition(
                control_id="positive-control",
                control_type=ControlType.POSITIVE,
                vessel_ids=positive_ids,
                nominal_level=design.control_level,
            )
        )

        materials.append(
            MaterialReference(
                material_id="check-standard",
                name="check_standard",
                properties={
                    "nominal_level": QuantityValue(
                        value=design.check_standard_level,
                        unit="dimensionless",
                    )
                },
            )
        )
        check_ids = self._add_replicates(
            vessels,
            source_by_vessel,
            prefix="check-standard",
            count=design.check_standard_replicates,
            material_id="check-standard",
            maximum_volume=maximum_volume,
        )
        controls.append(
            ControlDefinition(
                control_id="check-standard",
                control_type=ControlType.CHECK_STANDARD,
                vessel_ids=check_ids,
                nominal_level=design.check_standard_level,
            )
        )

        return tuple(vessels), tuple(controls), source_by_vessel, tuple(materials)

    def _add_replicates(
        self,
        vessels: list[VesselDefinition],
        source_by_vessel: dict[str, str],
        *,
        prefix: str,
        count: int,
        material_id: str,
        maximum_volume: QuantityValue,
    ) -> tuple[str, ...]:
        identifiers: list[str] = []
        for replicate in range(1, count + 1):
            vessel_id = f"{prefix}-{replicate:02d}"
            identifiers.append(vessel_id)
            source_by_vessel[vessel_id] = material_id
            vessels.append(
                VesselDefinition(
                    vessel_id=vessel_id,
                    vessel_type=self.vessel_type,
                    maximum_volume=maximum_volume,
                    location=_well_location(len(vessels)),
                )
            )
        return tuple(identifiers)

    def _steps(
        self,
        *,
        vessels: Iterable[VesselDefinition],
        source_by_vessel: dict[str, str],
        reagent_volume: QuantityValue,
        sample_volume: QuantityValue,
        incubation_time: QuantityValue,
        temperature: QuantityValue,
        wavelength: QuantityValue,
    ) -> tuple[ProtocolStep, ...]:
        vessel_list = tuple(vessels)
        total_volume = reagent_volume.value + sample_volume.value
        requested_mix_stroke = min(
            total_volume * self.mix_fraction,
            self.maximum_mix_stroke_microliter,
        )
        quantized_mix_stroke = (
            requested_mix_stroke / self.mix_resolution_microliter
        ).to_integral_value(rounding=ROUND_FLOOR) * self.mix_resolution_microliter
        mix_volume = QuantityValue(value=quantized_mix_stroke, unit="microliter")
        steps: list[ProtocolStep] = []
        for vessel in vessel_list:
            safe_id = vessel.vessel_id
            steps.extend(
                (
                    ProtocolStep(
                        step_id=f"dispense-reagent-{safe_id}",
                        command=DispenseCommand(
                            material_id="reagent",
                            destination_vessel_id=safe_id,
                            volume=reagent_volume,
                        ),
                        required_capability="liquid_handler",
                        timeout_seconds=Decimal("30"),
                        retry_safe=False,
                    ),
                    ProtocolStep(
                        step_id=f"dispense-sample-{safe_id}",
                        command=DispenseCommand(
                            material_id=source_by_vessel[safe_id],
                            destination_vessel_id=safe_id,
                            volume=sample_volume,
                        ),
                        required_capability="liquid_handler",
                        timeout_seconds=Decimal("30"),
                        retry_safe=False,
                    ),
                    ProtocolStep(
                        step_id=f"mix-{safe_id}",
                        command=MixCommand(
                            vessel_id=safe_id,
                            cycles=3,
                            volume=mix_volume,
                        ),
                        required_capability="liquid_handler",
                        timeout_seconds=Decimal("30"),
                        retry_safe=False,
                    ),
                )
            )
        vessel_ids = tuple(vessel.vessel_id for vessel in vessel_list)
        steps.extend(
            (
                ProtocolStep(
                    step_id="incubate-batch",
                    command=IncubateCommand(
                        vessel_ids=vessel_ids,
                        duration=incubation_time,
                        temperature=temperature,
                    ),
                    required_capability="incubator",
                    timeout_seconds=incubation_time.value * Decimal("60") + Decimal("30"),
                    retry_safe=False,
                ),
                ProtocolStep(
                    step_id="measure-absorbance-batch",
                    command=MeasureAbsorbanceCommand(
                        vessel_ids=vessel_ids,
                        wavelength=wavelength.to("nanometer"),
                    ),
                    required_capability="spectrophotometer",
                    timeout_seconds=Decimal("120"),
                    retry_safe=True,
                ),
            )
        )
        return tuple(steps)


def _well_location(index: int) -> str:
    """Map a zero-based batch position to a standard 96-well coordinate."""

    if not 0 <= index < 96:
        raise ValueError("V0 assay batch exceeds one 96-well plate")
    row = chr(ord("A") + index // 12)
    column = index % 12 + 1
    return f"{row}{column}"
