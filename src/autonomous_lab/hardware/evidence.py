"""Backend-neutral composition and finalization of executed laboratory evidence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from autonomous_lab.domain.measurements import Measurement
from autonomous_lab.domain.protocols import ExecutableProtocol, MeasureAbsorbanceCommand
from autonomous_lab.domain.quantities import QuantityValue
from autonomous_lab.hardware.commands import (
    AcquisitionRecord,
    EnvironmentalExposure,
    ExecutionEvidence,
    ExecutionEvidenceFragment,
    MaterialKind,
    ResourceLedgerEntry,
)
from autonomous_lab.provenance.hashing import canonical_sha256


class EvidenceFinalizationError(ValueError):
    """Device evidence fragments cannot form one internally consistent record."""


class EvidenceCostCalculator(Protocol):
    """Calculate costs from finalized actual conditions, never sensor internals."""

    def calculate(
        self,
        executed_parameters: Mapping[str, QuantityValue],
        *,
        executed_well_count: int,
    ) -> dict[str, QuantityValue]: ...


class EvidenceFinalizer(Protocol):
    def finalize(
        self,
        executable: ExecutableProtocol,
        *,
        run_id: UUID,
        fragments: tuple[ExecutionEvidenceFragment, ...],
        measurements: tuple[Measurement, ...],
        finalized_at: datetime,
    ) -> ExecutionEvidence: ...


@dataclass(frozen=True, slots=True)
class LinearAssayCostCalculator:
    """Configured assay cost model evaluated from actual finalized quantities."""

    fixed_consumables_per_well: Decimal
    reagent_per_milligram: Decimal
    sample_per_microliter: Decimal
    incubation_per_minute: Decimal
    currency_unit: str = "GBP"

    def calculate(
        self,
        executed_parameters: Mapping[str, QuantityValue],
        *,
        executed_well_count: int,
    ) -> dict[str, QuantityValue]:
        concentration = (
            executed_parameters["reagent_concentration"].to("milligram / milliliter").value
        )
        reagent_volume = executed_parameters["reagent_volume"].to("microliter").value
        sample_volume = executed_parameters["sample_volume"].to("microliter").value
        incubation = executed_parameters["incubation_time"].to("minute").value
        reagent_amount = concentration * reagent_volume / Decimal(1_000)
        cost_per_test = (
            self.fixed_consumables_per_well
            + self.reagent_per_milligram * reagent_amount
            + self.sample_per_microliter * sample_volume
            + self.incubation_per_minute * incubation
        )
        well_count = Decimal(executed_well_count)
        return {
            "cost_per_test": QuantityValue(value=cost_per_test, unit=self.currency_unit),
            "total_batch_cost": QuantityValue(
                value=cost_per_test * well_count,
                unit=self.currency_unit,
            ),
            "reagent_consumption_per_test": QuantityValue(
                value=reagent_amount,
                unit="milligram",
            ),
            "total_reagent_consumption": QuantityValue(
                value=reagent_amount * well_count,
                unit="milligram",
            ),
        }


@dataclass(frozen=True, slots=True)
class DefaultEvidenceFinalizer:
    """Compose independent device facts into one validated execution artifact."""

    cost_calculator: EvidenceCostCalculator | None = None

    def finalize(
        self,
        executable: ExecutableProtocol,
        *,
        run_id: UUID,
        fragments: tuple[ExecutionEvidenceFragment, ...],
        measurements: tuple[Measurement, ...],
        finalized_at: datetime,
    ) -> ExecutionEvidence:
        protocol = executable.protocol
        if not fragments:
            raise EvidenceFinalizationError("execution produced no evidence fragments")
        fragment_ids = [fragment.fragment_id for fragment in fragments]
        if len(fragment_ids) != len(set(fragment_ids)):
            raise EvidenceFinalizationError("execution contains duplicate evidence fragments")
        for fragment in fragments:
            if fragment.experiment_id != protocol.experiment_id or fragment.run_id != run_id:
                raise EvidenceFinalizationError("evidence fragment provenance does not match run")

        resources = self._finalize_resources(fragments)
        exposures = self._finalize_exposures(fragments)
        acquisitions = self._finalize_acquisitions(executable, fragments, measurements)
        executed_parameters = self._derive_executed_parameters(
            executable,
            resources,
            exposures,
            acquisitions,
        )
        costs = (
            {}
            if self.cost_calculator is None
            else self.cost_calculator.calculate(
                executed_parameters,
                executed_well_count=len(acquisitions),
            )
        )
        ordered_fragments = tuple(sorted(fragments, key=lambda item: str(item.fragment_id)))
        return ExecutionEvidence(
            evidence_id=uuid5(
                NAMESPACE_URL,
                f"autolab:execution-evidence:{run_id}:{canonical_sha256(fragment_ids)}",
            ),
            experiment_id=protocol.experiment_id,
            run_id=run_id,
            protocol_id=protocol.protocol_id,
            fragment_ids=tuple(item.fragment_id for item in ordered_fragments),
            resources=resources,
            exposures=exposures,
            acquisitions=acquisitions,
            executed_parameters=executed_parameters,
            costs=costs,
            created_at=finalized_at,
            metadata={
                "finalizer": type(self).__name__,
                "fragment_sources": [
                    {
                        "fragment_id": str(item.fragment_id),
                        "step_id": item.source_step_id,
                        "device_id": item.source_device_id,
                        "metadata": dict(item.metadata),
                    }
                    for item in ordered_fragments
                ],
            },
        )

    @staticmethod
    def _finalize_resources(
        fragments: tuple[ExecutionEvidenceFragment, ...],
    ) -> tuple[ResourceLedgerEntry, ...]:
        totals: dict[tuple[str, str], Decimal] = {}
        definitions: dict[
            tuple[str, str],
            tuple[MaterialKind, dict[str, QuantityValue]],
        ] = {}
        for fragment in fragments:
            for delta in fragment.resource_deltas:
                key = (delta.vessel_id, delta.material_id)
                definition = (delta.material_kind, dict(delta.properties))
                existing = definitions.get(key)
                if existing is not None and existing != definition:
                    raise EvidenceFinalizationError(
                        f"conflicting material facts for {delta.vessel_id!r}/{delta.material_id!r}"
                    )
                definitions[key] = definition
                totals[key] = (
                    totals.get(key, Decimal(0)) + delta.volume_delta.to("microliter").value
                )
        if any(volume < 0 for volume in totals.values()):
            raise EvidenceFinalizationError("resource ledger contains negative final volume")
        return tuple(
            ResourceLedgerEntry(
                vessel_id=vessel_id,
                material_id=material_id,
                material_kind=definitions[(vessel_id, material_id)][0],
                volume=QuantityValue(value=volume, unit="microliter"),
                properties=definitions[(vessel_id, material_id)][1],
            )
            for (vessel_id, material_id), volume in sorted(totals.items())
            if volume > 0
        )

    @staticmethod
    def _finalize_exposures(
        fragments: tuple[ExecutionEvidenceFragment, ...],
    ) -> tuple[EnvironmentalExposure, ...]:
        by_vessel: dict[str, tuple[QuantityValue, QuantityValue]] = {}
        for fragment in fragments:
            for exposure in fragment.exposures:
                for vessel_id in exposure.vessel_ids:
                    if vessel_id in by_vessel:
                        raise EvidenceFinalizationError(
                            f"duplicate environmental exposure for vessel {vessel_id!r}"
                        )
                    by_vessel[vessel_id] = (exposure.duration, exposure.temperature)
        grouped: dict[tuple[Decimal, Decimal], list[str]] = {}
        for vessel_id, (duration, temperature) in by_vessel.items():
            key = (
                duration.to("minute").value,
                temperature.to("degree_Celsius").value,
            )
            grouped.setdefault(key, []).append(vessel_id)
        return tuple(
            EnvironmentalExposure(
                vessel_ids=tuple(sorted(vessels)),
                duration=QuantityValue(value=duration, unit="minute"),
                temperature=QuantityValue(value=temperature, unit="degree_Celsius"),
            )
            for (duration, temperature), vessels in sorted(grouped.items())
        )

    @staticmethod
    def _finalize_acquisitions(
        executable: ExecutableProtocol,
        fragments: tuple[ExecutionEvidenceFragment, ...],
        measurements: tuple[Measurement, ...],
    ) -> tuple[AcquisitionRecord, ...]:
        acquisitions = tuple(
            acquisition for fragment in fragments for acquisition in fragment.acquisitions
        )
        identifiers = [item.acquisition_id for item in acquisitions]
        if len(identifiers) != len(set(identifiers)):
            raise EvidenceFinalizationError("duplicate acquisition identity")
        acquisition_keys = [(item.vessel_id, item.measurement_name) for item in acquisitions]
        if len(acquisition_keys) != len(set(acquisition_keys)):
            raise EvidenceFinalizationError("duplicate acquisition for vessel and measurement")
        expected = {
            (vessel_id, "absorbance")
            for step in executable.protocol.steps
            if isinstance(step.command, MeasureAbsorbanceCommand)
            for vessel_id in step.command.vessel_ids
        }
        if set(acquisition_keys) != expected:
            raise EvidenceFinalizationError("acquisitions do not match the validated protocol")
        successful = {item.acquisition_id for item in acquisitions if item.status == "ok"}
        if successful != {item.measurement_id for item in measurements}:
            raise EvidenceFinalizationError("acquisitions do not match persisted measurements")
        return tuple(
            sorted(
                acquisitions,
                key=lambda item: (
                    int(item.metadata.get("acquisition_index", 0)),
                    item.vessel_id,
                    str(item.acquisition_id),
                ),
            )
        )

    @staticmethod
    def _derive_executed_parameters(
        executable: ExecutableProtocol,
        resources: tuple[ResourceLedgerEntry, ...],
        exposures: tuple[EnvironmentalExposure, ...],
        acquisitions: tuple[AcquisitionRecord, ...],
    ) -> dict[str, QuantityValue]:
        resources_by_vessel: dict[str, list[ResourceLedgerEntry]] = {}
        for resource in resources:
            resources_by_vessel.setdefault(resource.vessel_id, []).append(resource)
        exposure_by_vessel = {
            vessel_id: exposure for exposure in exposures for vessel_id in exposure.vessel_ids
        }
        signatures: list[tuple[Decimal, Decimal, Decimal, Decimal, Decimal]] = []
        wavelengths: set[Decimal] = set()
        for acquisition in acquisitions:
            vessel_resources = resources_by_vessel.get(acquisition.vessel_id, [])
            reagents = [item for item in vessel_resources if item.material_kind == "reagent"]
            other = [item for item in vessel_resources if item.material_kind != "reagent"]
            if len(reagents) != 1 or not other:
                raise EvidenceFinalizationError(
                    f"vessel {acquisition.vessel_id!r} lacks one reagent and sample/control"
                )
            concentration = reagents[0].properties.get("concentration")
            if concentration is None:
                raise EvidenceFinalizationError("reagent concentration evidence is missing")
            exposure = exposure_by_vessel.get(acquisition.vessel_id)
            if exposure is None:
                raise EvidenceFinalizationError(
                    f"vessel {acquisition.vessel_id!r} lacks environmental exposure"
                )
            wavelength = acquisition.metadata.get("wavelength_nanometer")
            if isinstance(wavelength, bool) or not isinstance(wavelength, (Decimal, int, float)):
                raise EvidenceFinalizationError("acquisition wavelength evidence is invalid")
            wavelengths.add(Decimal(str(wavelength)))
            signatures.append(
                (
                    concentration.to("milligram / milliliter").value,
                    reagents[0].volume.to("microliter").value,
                    sum(
                        (item.volume.to("microliter").value for item in other),
                        Decimal(0),
                    ),
                    exposure.duration.to("minute").value,
                    exposure.temperature.to("degree_Celsius").value,
                )
            )
        if not signatures or any(item != signatures[0] for item in signatures[1:]):
            raise EvidenceFinalizationError("executed assay conditions are absent or inconsistent")
        if len(wavelengths) != 1:
            raise EvidenceFinalizationError("acquisition wavelengths are inconsistent")
        first = signatures[0]
        parameters = {
            "reagent_concentration": QuantityValue(value=first[0], unit="milligram / milliliter"),
            "reagent_volume": QuantityValue(value=first[1], unit="microliter"),
            "sample_volume": QuantityValue(value=first[2], unit="microliter"),
            "incubation_time": QuantityValue(value=first[3], unit="minute"),
            "temperature": QuantityValue(value=first[4], unit="degree_Celsius"),
            "wavelength": QuantityValue(value=next(iter(wavelengths)), unit="nanometer"),
        }
        proposed = executable.protocol.parameter_values
        for name in (
            "reagent_concentration",
            "reagent_volume",
            "sample_volume",
            "incubation_time",
            "temperature",
        ):
            expected = proposed.get(name)
            if (
                expected is not None
                and expected.to(parameters[name].unit).value != parameters[name].value
            ):
                parameters[f"requested_{name}"] = expected.canonical()
        return parameters
