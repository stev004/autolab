"""Assay metrics calculated from raw readings without implicit exclusions."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np

from autonomous_lab.analysis.calibration import CalibrationStatistics, fit_linear_calibration
from autonomous_lab.analysis.uncertainty import (
    UncertaintyEstimate,
    difference_of_means_uncertainty,
)
from autonomous_lab.simulation.assay_model import AssayBatch


@dataclass(frozen=True, slots=True)
class AnalysisReading:
    measurement_id: str
    role: str
    concentration: float
    response: float | None
    acquisition_index: int
    status: str = "ok"


@dataclass(frozen=True, slots=True)
class SummaryStatistics:
    count: int
    mean: float
    standard_deviation: float


@dataclass(frozen=True, slots=True)
class ControlStatistics:
    count: int
    mean: float
    standard_deviation: float
    coefficient_of_variation: float


@dataclass(frozen=True, slots=True)
class AssayAnalysis:
    blank: SummaryStatistics
    calibration: CalibrationStatistics
    control: ControlStatistics
    signal_to_noise: float
    sensitivity: float
    response_uncertainty: UncertaintyEstimate
    cost_per_test: float | None
    reagent_consumption_mg_per_test: float | None
    reagent_volume_microliter_per_test: float | None
    included_measurement_ids: tuple[str, ...]
    excluded_measurement_ids: tuple[str, ...]
    method_version: str = "assay-analysis-v1"

    def metric_values(self) -> dict[str, float]:
        values = {
            "blank_mean": self.blank.mean,
            "blank_sd": self.blank.standard_deviation,
            "calibration_slope": self.calibration.slope,
            "calibration_r_squared": self.calibration.r_squared,
            "calibration_slope_standard_error": self.calibration.slope_standard_error,
            "control_mean": self.control.mean,
            "control_sd": self.control.standard_deviation,
            "control_cv": self.control.coefficient_of_variation,
            "signal_to_noise": self.signal_to_noise,
            "sensitivity": self.sensitivity,
            "response_standard_uncertainty": self.response_uncertainty.standard_uncertainty,
        }
        if self.cost_per_test is not None:
            values["cost_per_test"] = self.cost_per_test
        if self.reagent_consumption_mg_per_test is not None:
            values["reagent_consumption"] = self.reagent_consumption_mg_per_test
        if self.reagent_volume_microliter_per_test is not None:
            values["reagent_volume"] = self.reagent_volume_microliter_per_test
        return values


def readings_from_simulation(batch: AssayBatch) -> tuple[AnalysisReading, ...]:
    return tuple(
        AnalysisReading(
            measurement_id=item.measurement_id,
            role=item.role.value,
            concentration=item.analyte_concentration,
            response=item.absorbance,
            acquisition_index=item.acquisition_index,
            status=item.status,
        )
        for item in batch.measurements
    )


def _statistics(values: list[float], label: str) -> SummaryStatistics:
    if not values:
        raise ValueError(f"no valid {label} readings")
    if any(not math.isfinite(value) for value in values):
        raise ValueError(f"{label} readings must be finite")
    standard_deviation = float(np.std(values, ddof=1)) if len(values) > 1 else 0.0
    return SummaryStatistics(len(values), float(np.mean(values)), standard_deviation)


def analyze_assay(
    records: Iterable[AnalysisReading],
    *,
    excluded_measurement_ids: Iterable[str] = (),
    cost_per_test: float | None = None,
    reagent_consumption_mg_per_test: float | None = None,
    reagent_volume_microliter_per_test: float | None = None,
) -> AssayAnalysis:
    """Compute V0 assay metrics.

    Missing readings are rejected.  Exclusions are accepted only through the
    explicit argument and are returned in the analysis artifact; this prevents a
    statistical helper from silently deleting suspected outliers.
    """

    excluded = frozenset(excluded_measurement_ids)
    materialized = tuple(records)
    evidence_ids = {item.measurement_id for item in materialized}
    unknown_exclusions = excluded - evidence_ids
    if unknown_exclusions:
        raise ValueError(
            "excluded measurement IDs are not part of the supplied evidence: "
            f"{sorted(unknown_exclusions)}"
        )
    duplicate_ids = len({item.measurement_id for item in materialized}) != len(materialized)
    if duplicate_ids:
        raise ValueError("measurement IDs must be unique")
    included = [item for item in materialized if item.measurement_id not in excluded]
    if any(item.status != "ok" or item.response is None for item in included):
        raise ValueError("analysis requires complete, successful measurements")

    grouped: dict[str, list[AnalysisReading]] = defaultdict(list)
    for item in included:
        grouped[item.role].append(item)
    blank_values = [float(item.response) for item in grouped["blank"] if item.response is not None]
    control_values = [
        float(item.response) for item in grouped["control"] if item.response is not None
    ]
    standards = grouped["standard"]
    if not standards:
        raise ValueError("no calibration standards")

    blank = _statistics(blank_values, "blank")
    control_summary = _statistics(control_values, "control")
    control_cv = (
        control_summary.standard_deviation / abs(control_summary.mean)
        if control_summary.mean != 0
        else math.inf
    )
    control = ControlStatistics(
        count=control_summary.count,
        mean=control_summary.mean,
        standard_deviation=control_summary.standard_deviation,
        coefficient_of_variation=control_cv,
    )
    calibration = fit_linear_calibration(
        [item.concentration for item in standards],
        [float(item.response) for item in standards if item.response is not None],
    )

    pooled_degrees = max(0, blank.count - 1) + max(0, control.count - 1)
    if pooled_degrees == 0:
        pooled_sd = 0.0
    else:
        pooled_variance = (
            max(0, blank.count - 1) * blank.standard_deviation**2
            + max(0, control.count - 1) * control.standard_deviation**2
        ) / pooled_degrees
        pooled_sd = math.sqrt(max(0.0, pooled_variance))
    signal_delta = control.mean - blank.mean
    signal_to_noise = (
        signal_delta / pooled_sd if pooled_sd > 0 else math.inf if signal_delta > 0 else 0.0
    )
    uncertainty = difference_of_means_uncertainty(
        first_standard_deviation=blank.standard_deviation,
        first_count=blank.count,
        second_standard_deviation=control.standard_deviation,
        second_count=control.count,
    )
    return AssayAnalysis(
        blank=blank,
        calibration=calibration,
        control=control,
        signal_to_noise=signal_to_noise,
        sensitivity=calibration.slope,
        response_uncertainty=uncertainty,
        cost_per_test=cost_per_test,
        reagent_consumption_mg_per_test=reagent_consumption_mg_per_test,
        reagent_volume_microliter_per_test=reagent_volume_microliter_per_test,
        included_measurement_ids=tuple(item.measurement_id for item in included),
        excluded_measurement_ids=tuple(sorted(excluded)),
    )


def analyze_simulated_batch(
    batch: AssayBatch, *, excluded_measurement_ids: Iterable[str] = ()
) -> AssayAnalysis:
    return analyze_assay(
        readings_from_simulation(batch),
        excluded_measurement_ids=excluded_measurement_ids,
        cost_per_test=batch.cost_per_test,
        reagent_consumption_mg_per_test=batch.reagent_amount_mg_per_test,
        reagent_volume_microliter_per_test=batch.reagent_volume_microliter_per_test,
    )
