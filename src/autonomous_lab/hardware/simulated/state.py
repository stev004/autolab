"""Shared state for a coherent simulated execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID

from autonomous_lab.domain.quantities import QuantityValue
from autonomous_lab.hardware.commands import (
    EnvironmentalExposure,
    MaterialKind,
    ResourceLedgerEntry,
)
from autonomous_lab.simulation.assay_model import ExecutedAssayConditions, FailureInjection
from autonomous_lab.simulation.assay_world import AssayWorld


@dataclass(slots=True)
class SimulatedRunState:
    experiment_id: UUID
    world: AssayWorld
    failures: FailureInjection
    vessel_volumes_microliter: dict[str, Decimal] = field(default_factory=lambda: {})
    vessel_materials: dict[str, set[str]] = field(default_factory=lambda: {})
    vessel_material_volumes_microliter: dict[str, dict[str, Decimal]] = field(
        default_factory=lambda: {}
    )
    material_kinds: dict[str, MaterialKind] = field(default_factory=lambda: {})
    material_properties: dict[str, dict[str, QuantityValue]] = field(default_factory=lambda: {})
    incubated_vessels: set[str] = field(default_factory=lambda: set())
    incubation_exposures: dict[str, tuple[Decimal, Decimal]] = field(default_factory=lambda: {})
    mixed_vessels: set[str] = field(default_factory=lambda: set())
    ambiguous_dispense_injected: bool = False
    incubation_failure_injected: bool = False
    spectrophotometer_timeout_injected: bool = False
    safe_state_devices: set[str] = field(default_factory=lambda: set())

    def volume(self, vessel_id: str) -> Decimal:
        return self.vessel_volumes_microliter.get(vessel_id, Decimal(0))

    def dispense(self, vessel_id: str, material_id: str, amount: Decimal) -> None:
        materials = self.vessel_material_volumes_microliter.setdefault(vessel_id, {})
        materials[material_id] = materials.get(material_id, Decimal(0)) + amount
        self._synchronize_vessel(vessel_id)

    def aspirate(self, vessel_id: str, amount: Decimal) -> dict[str, Decimal]:
        current = self.volume(vessel_id)
        if amount > current:
            raise ValueError("aspirated volume exceeds vessel contents")
        if current == 0:
            return {}
        removed: dict[str, Decimal] = {}
        materials = self.vessel_material_volumes_microliter.setdefault(vessel_id, {})
        for material_id, material_volume in tuple(materials.items()):
            material_removed = amount * material_volume / current
            removed[material_id] = material_removed
            remaining = material_volume - material_removed
            if remaining == 0:
                del materials[material_id]
            else:
                materials[material_id] = remaining
        self._synchronize_vessel(vessel_id)
        return removed

    def transfer(
        self,
        source_vessel_id: str,
        destination_vessel_id: str,
        amount: Decimal,
    ) -> dict[str, Decimal]:
        removed = self.aspirate(source_vessel_id, amount)
        destination = self.vessel_material_volumes_microliter.setdefault(destination_vessel_id, {})
        for material_id, material_volume in removed.items():
            destination[material_id] = destination.get(material_id, Decimal(0)) + material_volume
        self._synchronize_vessel(destination_vessel_id)
        return removed

    def record_incubation(
        self,
        vessel_ids: tuple[str, ...],
        *,
        duration_minutes: Decimal,
        temperature_celsius: Decimal,
    ) -> None:
        for vessel_id in vessel_ids:
            self.incubation_exposures[vessel_id] = (
                duration_minutes,
                temperature_celsius,
            )
        self.incubated_vessels.update(vessel_ids)

    def executed_conditions(
        self,
        vessel_ids: tuple[str, ...],
    ) -> ExecutedAssayConditions:
        """Derive and cross-check conditions solely from successful operations."""

        if not vessel_ids:
            raise ValueError("at least one measured vessel is required")
        signatures: list[tuple[Decimal, Decimal, Decimal, Decimal, Decimal]] = []
        for vessel_id in vessel_ids:
            if vessel_id not in self.mixed_vessels:
                raise ValueError(f"vessel {vessel_id!r} was not mixed")
            exposure = self.incubation_exposures.get(vessel_id)
            if exposure is None:
                raise ValueError(f"vessel {vessel_id!r} was not incubated")
            materials = self.vessel_material_volumes_microliter.get(vessel_id, {})
            reagent_volume = materials.get("reagent", Decimal(0))
            if reagent_volume <= 0:
                raise ValueError(f"vessel {vessel_id!r} contains no reagent")
            sample_volume = sum(
                (volume for material, volume in materials.items() if material != "reagent"),
                Decimal(0),
            )
            if sample_volume <= 0:
                raise ValueError(f"vessel {vessel_id!r} contains no sample or control")
            concentration = self.material_properties.get("reagent", {}).get("concentration")
            if concentration is None:
                raise ValueError("reagent concentration is absent from the material ledger")
            concentration_value = concentration.to("milligram / milliliter").value
            duration, temperature = exposure
            signatures.append(
                (
                    concentration_value,
                    reagent_volume,
                    sample_volume,
                    duration,
                    temperature,
                )
            )
        first = signatures[0]
        if any(signature != first for signature in signatures[1:]):
            raise ValueError("measured vessels have inconsistent executed conditions")
        return ExecutedAssayConditions(
            reagent_concentration_mg_ml=float(first[0]),
            reagent_volume_microliter=float(first[1]),
            sample_volume_microliter=float(first[2]),
            incubation_minutes=float(first[3]),
            temperature_celsius=float(first[4]),
        )

    def resource_ledger(
        self,
        vessel_ids: tuple[str, ...],
    ) -> tuple[ResourceLedgerEntry, ...]:
        entries: list[ResourceLedgerEntry] = []
        for vessel_id in sorted(vessel_ids):
            for material_id, volume in sorted(
                self.vessel_material_volumes_microliter.get(vessel_id, {}).items()
            ):
                entries.append(
                    ResourceLedgerEntry(
                        vessel_id=vessel_id,
                        material_id=material_id,
                        material_kind=self.material_kinds.get(material_id, "other"),
                        volume=QuantityValue(value=volume, unit="microliter"),
                        properties=dict(self.material_properties.get(material_id, {})),
                    )
                )
        return tuple(entries)

    def environmental_exposures(
        self,
        vessel_ids: tuple[str, ...],
    ) -> tuple[EnvironmentalExposure, ...]:
        grouped: dict[tuple[Decimal, Decimal], list[str]] = {}
        for vessel_id in vessel_ids:
            exposure = self.incubation_exposures.get(vessel_id)
            if exposure is None:
                continue
            grouped.setdefault(exposure, []).append(vessel_id)
        return tuple(
            EnvironmentalExposure(
                vessel_ids=tuple(sorted(group_vessels)),
                duration=QuantityValue(value=duration, unit="minute"),
                temperature=QuantityValue(value=temperature, unit="degree_Celsius"),
            )
            for (duration, temperature), group_vessels in sorted(grouped.items())
        )

    def _synchronize_vessel(self, vessel_id: str) -> None:
        materials = self.vessel_material_volumes_microliter.get(vessel_id, {})
        positive = {material_id: volume for material_id, volume in materials.items() if volume > 0}
        self.vessel_material_volumes_microliter[vessel_id] = positive
        self.vessel_materials[vessel_id] = set(positive)
        self.vessel_volumes_microliter[vessel_id] = sum(
            positive.values(),
            Decimal(0),
        )
