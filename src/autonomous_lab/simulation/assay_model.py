"""Hidden nonlinear colorimetric assay model used by the V0 simulator.

The public boundary accepts *executed* physical quantities in canonical units and
returns raw, instrument-like well readings.  Scientific metrics are intentionally
computed in :mod:`autonomous_lab.analysis`, not here.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final, Literal

from autonomous_lab.simulation.model_versions import (
    ASSAY_MODEL_ID,
    ASSAY_MODEL_VERSION,
    ASSAY_PARAMETER_DOMAIN,
)
from autonomous_lab.simulation.random_streams import independent_rng, stream_identifier


class WellRole(StrEnum):
    BLANK = "blank"
    STANDARD = "standard"
    CONTROL = "control"
    CHECK_STANDARD = "check_standard"


@dataclass(frozen=True, slots=True)
class ExecutedAssayConditions:
    """Actual conditions used by the laboratory, in the documented units."""

    reagent_concentration_mg_ml: float
    reagent_volume_microliter: float
    sample_volume_microliter: float
    incubation_minutes: float
    temperature_celsius: float

    def __post_init__(self) -> None:
        values = asdict(self)
        if not all(math.isfinite(value) for value in values.values()):
            raise ValueError("executed conditions must be finite")
        if self.reagent_concentration_mg_ml <= 0:
            raise ValueError("reagent concentration must be positive")
        if self.reagent_volume_microliter <= 0 or self.sample_volume_microliter <= 0:
            raise ValueError("executed volumes must be positive")
        if self.incubation_minutes < 0:
            raise ValueError("incubation time cannot be negative")

    @property
    def reagent_amount_mg_per_well(self) -> float:
        return self.reagent_concentration_mg_ml * self.reagent_volume_microliter / 1_000.0


@dataclass(frozen=True, slots=True)
class AssayDesign:
    blank_replicates: int = 3
    standard_levels: tuple[float, ...] = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
    standard_replicates: int = 3
    control_level: float = 0.5
    control_replicates: int = 4
    check_standard_level: float | None = 0.7
    check_standard_replicates: int = 1
    wavelength_nanometer: float = 520.0

    def __post_init__(self) -> None:
        if self.blank_replicates < 3:
            raise ValueError("at least three blanks are required")
        if len(set(self.standard_levels)) < 2:
            raise ValueError("at least two distinct standard levels are required")
        if self.standard_replicates < 2 or self.control_replicates < 3:
            raise ValueError("insufficient assay replicates")
        if self.check_standard_replicates < 1:
            raise ValueError("check-standard replicates must be positive")
        if any(level < 0 or not math.isfinite(level) for level in self.standard_levels):
            raise ValueError("standard levels must be finite and non-negative")
        if self.control_level < 0 or self.wavelength_nanometer <= 0:
            raise ValueError("control level and wavelength must be valid")


@dataclass(frozen=True, slots=True)
class CostModel:
    currency: str = "GBP"
    fixed_consumables_per_well: float = 0.018
    reagent_per_mg: float = 1.80
    sample_per_microliter: float = 0.00015
    incubation_per_minute: float = 0.0002

    def __post_init__(self) -> None:
        if not self.currency:
            raise ValueError("currency must not be empty")
        numeric = (
            self.fixed_consumables_per_well,
            self.reagent_per_mg,
            self.sample_per_microliter,
            self.incubation_per_minute,
        )
        if any(not math.isfinite(value) or value < 0 for value in numeric):
            raise ValueError("cost coefficients must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class FailureInjection:
    """Explicit simulator faults used to exercise safety and QC behavior."""

    missing_well_id: str | None = None
    single_outlier_well_id: str | None = None
    contaminated_blank: bool = False
    excessive_drift: bool = False
    spectrophotometer_timeout: bool = False
    failed_incubation: bool = False
    ambiguous_dispense_failure: bool = False


@dataclass(frozen=True, slots=True)
class SimulationConfig:
    seed: int = 73_109
    outlier_probability: float = 0.015
    gaussian_noise_sd: float = 0.004
    well_effect_sd: float = 0.0025
    batch_drift_sd: float = 0.008
    outlier_scale: float = 0.22
    blank_offset: float = 0.018
    detector_max_absorbance: float = 3.5
    cost: CostModel = field(default_factory=CostModel)

    def __post_init__(self) -> None:
        if self.seed < 0:
            raise ValueError("seed must be non-negative")
        if not 0 <= self.outlier_probability <= 1:
            raise ValueError("outlier probability must be between zero and one")
        non_negative = (
            self.gaussian_noise_sd,
            self.well_effect_sd,
            self.batch_drift_sd,
            self.outlier_scale,
            self.blank_offset,
        )
        if any(not math.isfinite(value) or value < 0 for value in non_negative):
            raise ValueError("noise and response settings must be finite and non-negative")
        if self.detector_max_absorbance <= 0:
            raise ValueError("detector maximum must be positive")

    def provenance_hash(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


MeasurementStatus = Literal["ok", "missing"]


@dataclass(frozen=True, slots=True)
class RawAssayMeasurement:
    measurement_id: str
    experiment_id: str
    well_id: str
    role: WellRole
    analyte_concentration: float
    replicate: int
    acquisition_index: int
    absorbance: float | None
    wavelength_nanometer: float
    status: MeasurementStatus
    stream_id: str


@dataclass(frozen=True, slots=True)
class AssayBatch:
    experiment_id: str
    experiment_ordinal: int
    campaign_seed: int
    model_id: str
    model_version: str
    simulator_config_hash: str
    conditions: ExecutedAssayConditions
    measurements: tuple[RawAssayMeasurement, ...]
    executed_well_count: int
    reagent_amount_mg_per_test: float
    reagent_volume_microliter_per_test: float
    total_reagent_consumption_mg: float
    total_reagent_volume_microliter: float
    cost_per_test: float
    total_batch_cost: float
    currency: str
    provenance: MappingProxyType[str, Any]


@dataclass(frozen=True, slots=True)
class _PlannedWell:
    well_id: str
    role: WellRole
    level: float
    replicate: int


class _ResponseSurface:
    """Private hidden response surface; never returned to an optimiser."""

    # These broad operating bounds normalise the V0 parameter space.  Validation is
    # responsible for rejecting out-of-range proposals before simulation.
    _C_RANGE: Final = tuple(
        float(value)
        for value in (
            ASSAY_PARAMETER_DOMAIN["reagent_concentration"].minimum,
            ASSAY_PARAMETER_DOMAIN["reagent_concentration"].maximum,
        )
    )
    _RV_RANGE: Final = tuple(
        float(value)
        for value in (
            ASSAY_PARAMETER_DOMAIN["reagent_volume"].minimum,
            ASSAY_PARAMETER_DOMAIN["reagent_volume"].maximum,
        )
    )
    _SV_RANGE: Final = tuple(
        float(value)
        for value in (
            ASSAY_PARAMETER_DOMAIN["sample_volume"].minimum,
            ASSAY_PARAMETER_DOMAIN["sample_volume"].maximum,
        )
    )

    @staticmethod
    def _normalise(value: float, low: float, high: float) -> float:
        return min(1.0, max(0.0, (value - low) / (high - low)))

    def sensitivity_and_noise(self, conditions: ExecutedAssayConditions) -> tuple[float, float]:
        concentration = self._normalise(conditions.reagent_concentration_mg_ml, *self._C_RANGE)
        reagent_volume = self._normalise(conditions.reagent_volume_microliter, *self._RV_RANGE)
        sample_volume = self._normalise(conditions.sample_volume_microliter, *self._SV_RANGE)

        amount = conditions.reagent_amount_mg_per_well
        reagent_saturation = 1.0 - math.exp(-amount / 0.038)
        ratio = conditions.sample_volume_microliter / conditions.reagent_volume_microliter
        ratio_efficiency = 0.54 + 0.46 * math.exp(-0.5 * ((ratio - 0.58) / 0.48) ** 2)

        temperature_efficiency = 0.38 + 0.62 * math.exp(
            -0.5 * ((conditions.temperature_celsius - 29.5) / 6.2) ** 2
        )
        kinetic_tau = 8.2 / max(0.45, temperature_efficiency)
        kinetic_completion = 1.0 - math.exp(-conditions.incubation_minutes / kinetic_tau)

        concentration_volume_interaction = 0.82 + 0.18 * math.sqrt(
            max(0.0, concentration * reagent_volume)
        )
        sample_interaction = 0.88 + 0.12 * sample_volume * (1.0 - 0.45 * reagent_volume)
        sensitivity = (
            2.95
            * reagent_saturation
            * ratio_efficiency
            * temperature_efficiency
            * kinetic_completion
            * concentration_volume_interaction
            * sample_interaction
        )

        # Poor operating regions have intrinsically worse precision.  Low volumes
        # are particularly punishing, which prevents a zero-volume cost optimum.
        low_volume_penalty = (
            0.020 * (1.0 - reagent_volume) ** 2 + 0.013 * (1.0 - sample_volume) ** 2
        )
        poor_region_penalty = 0.014 * (1.0 - temperature_efficiency) + 0.010 * (
            1.0 - ratio_efficiency
        )
        relative_noise = low_volume_penalty + poor_region_penalty
        return sensitivity, relative_noise

    @staticmethod
    def expected_absorbance(level: float, sensitivity: float, blank_offset: float) -> float:
        # A small quadratic term and rational detector saturation make calibration
        # quality depend on recipe sensitivity, rather than being universally linear.
        linear_signal = sensitivity * level
        saturated_signal = linear_signal / (1.0 + 0.17 * linear_signal)
        curvature = 0.012 * sensitivity * level * level
        return blank_offset + saturated_signal + curvature


class NonlinearAssaySimulator:
    """Generate a complete assay batch without exposing the hidden model."""

    def __init__(self, config: SimulationConfig | None = None) -> None:
        self.config = config or SimulationConfig()
        self._surface = _ResponseSurface()

    def simulate_batch(
        self,
        *,
        experiment_id: str,
        experiment_ordinal: int,
        campaign_seed: int,
        conditions: ExecutedAssayConditions,
        design: AssayDesign | None = None,
        failures: FailureInjection | None = None,
    ) -> AssayBatch:
        if not experiment_id:
            raise ValueError("experiment_id must not be empty")
        if experiment_ordinal < 0 or campaign_seed < 0:
            raise ValueError("ordinal and campaign seed must be non-negative")
        assay_design = design or AssayDesign()
        injection = failures or FailureInjection()
        if injection.failed_incubation:
            raise SimulationExecutionError("failed_incubation", retry_safe=False)
        if injection.ambiguous_dispense_failure:
            raise SimulationExecutionError("ambiguous_dispense", retry_safe=False, ambiguous=True)
        if injection.spectrophotometer_timeout:
            raise SimulationExecutionError("spectrophotometer_timeout", retry_safe=True)

        planned = self._plan_wells(assay_design)
        sensitivity, region_noise = self._surface.sensitivity_and_noise(conditions)
        drift_rng = independent_rng(
            campaign_seed,
            self.config.seed,
            experiment_ordinal,
            "batch_drift",
        )
        batch_drift = float(drift_rng.normal(0.0, self.config.batch_drift_sd))
        if injection.excessive_drift:
            batch_drift = math.copysign(max(abs(batch_drift), 0.14), batch_drift or 1.0)

        readings: list[RawAssayMeasurement] = []
        last_index = max(1, len(planned) - 1)
        for index, well in enumerate(planned):
            expected = self._surface.expected_absorbance(
                well.level, sensitivity, self.config.blank_offset
            )
            if injection.contaminated_blank and well.role is WellRole.BLANK:
                expected += 0.18

            centered_position = (index / last_index) - 0.5
            drift = batch_drift * centered_position
            noise_scale = self.config.gaussian_noise_sd + region_noise + 0.008 * max(expected, 0.0)
            noise_rng = independent_rng(
                campaign_seed,
                self.config.seed,
                experiment_ordinal,
                well.well_id,
                "measurement_noise",
            )
            well_rng = independent_rng(
                campaign_seed,
                self.config.seed,
                experiment_ordinal,
                well.well_id,
                "well_effect",
            )
            observed = expected + drift
            observed += float(noise_rng.normal(0.0, noise_scale))
            observed += float(well_rng.normal(0.0, self.config.well_effect_sd))

            outlier_rng = independent_rng(
                campaign_seed,
                self.config.seed,
                experiment_ordinal,
                well.well_id,
                "outlier_event",
            )
            is_outlier = bool(outlier_rng.random() < self.config.outlier_probability)
            if injection.single_outlier_well_id == well.well_id:
                is_outlier = True
            if is_outlier:
                sign = -1.0 if outlier_rng.random() < 0.5 else 1.0
                observed += sign * self.config.outlier_scale * (0.8 + float(outlier_rng.random()))

            status: MeasurementStatus = "ok"
            absorbance: float | None = min(
                self.config.detector_max_absorbance, max(-0.05, observed)
            )
            if injection.missing_well_id == well.well_id:
                status = "missing"
                absorbance = None

            measurement_id = f"{experiment_id}:{well.well_id}:absorbance"
            readings.append(
                RawAssayMeasurement(
                    measurement_id=measurement_id,
                    experiment_id=experiment_id,
                    well_id=well.well_id,
                    role=well.role,
                    analyte_concentration=well.level,
                    replicate=well.replicate,
                    acquisition_index=index,
                    absorbance=absorbance,
                    wavelength_nanometer=assay_design.wavelength_nanometer,
                    status=status,
                    stream_id=stream_identifier(
                        campaign_seed,
                        self.config.seed,
                        experiment_ordinal,
                        well.well_id,
                    ),
                )
            )

        well_count = len(planned)
        amount_per_well = conditions.reagent_amount_mg_per_well
        reagent_consumption = amount_per_well * well_count
        reagent_volume = conditions.reagent_volume_microliter * well_count
        cost_per_test = self._cost_per_well(conditions)
        provenance = MappingProxyType(
            {
                "seed": self.config.seed,
                "campaign_seed": campaign_seed,
                "experiment_ordinal": experiment_ordinal,
                "random_stream_namespace": stream_identifier(
                    campaign_seed, self.config.seed, experiment_ordinal
                ),
            }
        )
        return AssayBatch(
            experiment_id=experiment_id,
            experiment_ordinal=experiment_ordinal,
            campaign_seed=campaign_seed,
            model_id=ASSAY_MODEL_ID,
            model_version=ASSAY_MODEL_VERSION,
            simulator_config_hash=self.config.provenance_hash(),
            conditions=conditions,
            measurements=tuple(readings),
            executed_well_count=well_count,
            reagent_amount_mg_per_test=amount_per_well,
            reagent_volume_microliter_per_test=conditions.reagent_volume_microliter,
            total_reagent_consumption_mg=reagent_consumption,
            total_reagent_volume_microliter=reagent_volume,
            cost_per_test=cost_per_test,
            total_batch_cost=cost_per_test * well_count,
            currency=self.config.cost.currency,
            provenance=provenance,
        )

    def _cost_per_well(self, conditions: ExecutedAssayConditions) -> float:
        costs = self.config.cost
        return (
            costs.fixed_consumables_per_well
            + costs.reagent_per_mg * conditions.reagent_amount_mg_per_well
            + costs.sample_per_microliter * conditions.sample_volume_microliter
            + costs.incubation_per_minute * conditions.incubation_minutes
        )

    @staticmethod
    def _plan_wells(design: AssayDesign) -> tuple[_PlannedWell, ...]:
        non_blanks: list[_PlannedWell] = []
        for level_index, level in enumerate(design.standard_levels):
            for replicate in range(1, design.standard_replicates + 1):
                non_blanks.append(
                    _PlannedWell(
                        f"STD-{level_index + 1:02d}-{replicate:02d}",
                        WellRole.STANDARD,
                        level,
                        replicate,
                    )
                )
        for replicate in range(1, design.control_replicates + 1):
            non_blanks.append(
                _PlannedWell(
                    f"CTL-{replicate:02d}", WellRole.CONTROL, design.control_level, replicate
                )
            )
        if design.check_standard_level is not None:
            for replicate in range(1, design.check_standard_replicates + 1):
                non_blanks.append(
                    _PlannedWell(
                        f"CHK-{replicate:02d}",
                        WellRole.CHECK_STANDARD,
                        design.check_standard_level,
                        replicate,
                    )
                )

        # Disperse blanks through the acquisition order so their slope can reveal
        # batch drift.  Stable insertion points also make seeded results reproducible.
        planned = list(non_blanks)
        for zero_based in range(design.blank_replicates):
            insertion = round(zero_based * len(planned) / max(1, design.blank_replicates - 1))
            planned.insert(
                min(insertion + zero_based, len(planned)),
                _PlannedWell(f"BLK-{zero_based + 1:02d}", WellRole.BLANK, 0.0, zero_based + 1),
            )
        return tuple(planned)


class SimulationExecutionError(RuntimeError):
    def __init__(self, failure_kind: str, *, retry_safe: bool, ambiguous: bool = False) -> None:
        super().__init__(failure_kind)
        self.failure_kind = failure_kind
        self.retry_safe = retry_safe
        self.ambiguous = ambiguous
