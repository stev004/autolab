"""Deterministic run-integrity quality control.

Integrity checks answer whether a run produced trustworthy evidence.  They do not
enforce assay-performance targets such as CV <= 5% or R-squared >= 0.99; those
scientifically useful failures belong in constrained optimisation.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from autonomous_lab.analysis.calibration import fit_linear_calibration
from autonomous_lab.analysis.metrics import AnalysisReading
from autonomous_lab.qc.outliers import OutlierFlag, flag_modified_z_score_outliers


def _empty_details() -> dict[str, Any]:
    return {}


@dataclass(frozen=True, slots=True)
class IntegrityQCConfig:
    minimum_blank_replicates: int = 3
    minimum_standard_replicates: int = 3
    minimum_control_replicates: int = 3
    minimum_check_standard_replicates: int = 1
    minimum_distinct_standard_levels: int = 6
    blank_absorbance_maximum: float = 0.12
    control_absorbance_minimum: float = -0.05
    control_absorbance_maximum: float = 3.25
    check_standard_recovery_minimum: float = 0.6
    check_standard_recovery_maximum: float = 1.4
    detector_saturation_absorbance: float = 3.5
    maximum_blank_drift: float = 0.1
    outlier_modified_z_threshold: float = 3.5
    fail_on_flagged_outlier: bool = False

    def __post_init__(self) -> None:
        replicate_counts = (
            self.minimum_blank_replicates,
            self.minimum_standard_replicates,
            self.minimum_control_replicates,
            self.minimum_check_standard_replicates,
            self.minimum_distinct_standard_levels,
        )
        if any(count < 1 for count in replicate_counts):
            raise ValueError("required replicate and level counts must be positive")
        non_negative_limits = (
            self.blank_absorbance_maximum,
            self.control_absorbance_maximum,
            self.check_standard_recovery_minimum,
            self.check_standard_recovery_maximum,
            self.detector_saturation_absorbance,
            self.maximum_blank_drift,
            self.outlier_modified_z_threshold,
        )
        if any(not math.isfinite(value) or value < 0 for value in non_negative_limits):
            raise ValueError("QC limits must be finite and non-negative")
        if not math.isfinite(self.control_absorbance_minimum):
            raise ValueError("control minimum must be finite")
        if self.control_absorbance_minimum > self.control_absorbance_maximum:
            raise ValueError("control integrity range is inverted")
        if self.check_standard_recovery_minimum > self.check_standard_recovery_maximum:
            raise ValueError("check-standard recovery range is inverted")


@dataclass(frozen=True, slots=True)
class QCCheck:
    code: str
    passed: bool
    message: str
    measurement_ids: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=_empty_details)


@dataclass(frozen=True, slots=True)
class IntegrityQCReport:
    passed: bool
    optimizer_eligible: bool
    checks: tuple[QCCheck, ...]
    outlier_flags: tuple[OutlierFlag, ...]
    method_version: str = "integrity-qc-v1"

    @property
    def failed_check_codes(self) -> tuple[str, ...]:
        return tuple(check.code for check in self.checks if not check.passed)


def _check(code: str, passed: bool, success: str, failure: str, **details: Any) -> QCCheck:
    return QCCheck(
        code=code, passed=passed, message=success if passed else failure, details=details
    )


def _drift_amplitude(blank_records: list[AnalysisReading]) -> float | None:
    if len(blank_records) < 2:
        return None
    x = np.asarray([item.acquisition_index for item in blank_records], dtype=float)
    y = np.asarray(
        [float(item.response) for item in blank_records if item.response is not None], dtype=float
    )
    if len(x) != len(y) or np.ptp(x) == 0:
        return None
    slope = float(np.polyfit(x, y, 1)[0])
    return abs(slope * float(np.ptp(x)))


def evaluate_integrity_qc(
    records: Iterable[AnalysisReading], config: IntegrityQCConfig | None = None
) -> IntegrityQCReport:
    policy = config or IntegrityQCConfig()
    materialized = tuple(records)
    checks: list[QCCheck] = []
    identifiers = [item.measurement_id for item in materialized]
    checks.append(
        _check(
            "measurement_ids_unique",
            len(identifiers) == len(set(identifiers)),
            "measurement IDs are unique",
            "duplicate measurement IDs detected",
        )
    )
    missing = tuple(
        item.measurement_id
        for item in materialized
        if item.status != "ok" or item.response is None or not math.isfinite(float(item.response))
    )
    checks.append(
        QCCheck(
            code="measurements_complete",
            passed=not missing,
            message="all required measurements are present"
            if not missing
            else "missing or invalid readings detected",
            measurement_ids=missing,
            details={"missing_count": len(missing)},
        )
    )
    usable = [
        item
        for item in materialized
        if item.status == "ok" and item.response is not None and math.isfinite(float(item.response))
    ]
    grouped: dict[str, list[AnalysisReading]] = defaultdict(list)
    for item in usable:
        grouped[item.role].append(item)

    blank_records = grouped["blank"]
    control_records = grouped["control"]
    standard_records = grouped["standard"]
    check_standard_records = grouped["check_standard"]
    standard_counts = Counter(item.concentration for item in standard_records)
    blank_count_ok = len(blank_records) >= policy.minimum_blank_replicates
    checks.append(
        _check(
            "blank_replicates",
            blank_count_ok,
            "sufficient blank replicates",
            "insufficient blank replicates",
            observed=len(blank_records),
            required=policy.minimum_blank_replicates,
        )
    )
    check_standard_count_ok = (
        len(check_standard_records) >= policy.minimum_check_standard_replicates
    )
    checks.append(
        _check(
            "check_standard_replicates",
            check_standard_count_ok,
            "sufficient independent check-standard replicates",
            "insufficient independent check-standard replicates",
            observed=len(check_standard_records),
            required=policy.minimum_check_standard_replicates,
        )
    )
    control_count_ok = len(control_records) >= policy.minimum_control_replicates
    checks.append(
        _check(
            "control_replicates",
            control_count_ok,
            "sufficient control replicates",
            "insufficient control replicates",
            observed=len(control_records),
            required=policy.minimum_control_replicates,
        )
    )
    distinct_ok = len(standard_counts) >= policy.minimum_distinct_standard_levels
    replicate_ok = distinct_ok and all(
        count >= policy.minimum_standard_replicates for count in standard_counts.values()
    )
    checks.append(
        _check(
            "standard_replicates",
            replicate_ok,
            "sufficient calibration levels and replicates",
            "insufficient calibration levels or replicates",
            observed_levels=len(standard_counts),
            required_levels=policy.minimum_distinct_standard_levels,
            counts=dict(sorted(standard_counts.items())),
            required_replicates=policy.minimum_standard_replicates,
        )
    )

    blank_values = [float(item.response) for item in blank_records if item.response is not None]
    blank_mean = float(np.mean(blank_values)) if blank_values else math.nan
    blank_ok = bool(blank_values) and blank_mean <= policy.blank_absorbance_maximum
    checks.append(
        _check(
            "blank_contamination",
            blank_ok,
            "blank signal is within integrity limit",
            "blank signal indicates possible contamination",
            observed_mean=blank_mean,
            maximum=policy.blank_absorbance_maximum,
        )
    )
    control_values = [float(item.response) for item in control_records if item.response is not None]
    control_mean = float(np.mean(control_values)) if control_values else math.nan
    control_ok = bool(control_values) and (
        policy.control_absorbance_minimum <= control_mean <= policy.control_absorbance_maximum
    )
    checks.append(
        _check(
            "control_integrity_range",
            control_ok,
            "control signal is within broad integrity range",
            "control signal is outside broad integrity range",
            observed_mean=control_mean,
            minimum=policy.control_absorbance_minimum,
            maximum=policy.control_absorbance_maximum,
        )
    )
    saturated = tuple(
        item.measurement_id
        for item in usable
        if item.response is not None and item.response >= policy.detector_saturation_absorbance
    )
    checks.append(
        QCCheck(
            code="detector_not_saturated",
            passed=not saturated,
            message="detector readings are unsaturated"
            if not saturated
            else "one or more readings are saturated",
            measurement_ids=saturated,
            details={"saturation_absorbance": policy.detector_saturation_absorbance},
        )
    )

    calibration_computable = False
    calibration = None
    calibration_error: str | None = None
    try:
        calibration = fit_linear_calibration(
            [item.concentration for item in standard_records],
            [float(item.response) for item in standard_records if item.response is not None],
        )
        calibration_computable = True
    except ValueError as error:
        calibration_error = str(error)
    checks.append(
        _check(
            "calibration_computable",
            calibration_computable,
            "calibration can be computed",
            "calibration cannot be computed",
            error=calibration_error,
        )
    )

    check_recovery: float | None = None
    if calibration is not None and check_standard_records:
        recoveries: list[float] = []
        for record in check_standard_records:
            if record.response is None:
                continue
            predicted = calibration.intercept + calibration.slope * record.concentration
            if predicted > 0:
                recoveries.append(float(record.response) / predicted)
        if recoveries:
            check_recovery = float(np.mean(recoveries))
    check_recovery_ok = check_recovery is not None and (
        policy.check_standard_recovery_minimum
        <= check_recovery
        <= policy.check_standard_recovery_maximum
    )
    checks.append(
        _check(
            "check_standard_recovery",
            check_recovery_ok,
            "independent check-standard recovery is within integrity range",
            "independent check-standard recovery is outside integrity range or unavailable",
            observed_recovery=check_recovery,
            minimum=policy.check_standard_recovery_minimum,
            maximum=policy.check_standard_recovery_maximum,
        )
    )

    drift = _drift_amplitude(blank_records)
    drift_ok = drift is not None and drift <= policy.maximum_blank_drift
    checks.append(
        _check(
            "blank_drift",
            drift_ok,
            "blank drift is within integrity limit",
            "blank drift is excessive or cannot be estimated",
            observed_amplitude=drift,
            maximum=policy.maximum_blank_drift,
        )
    )

    outlier_flags: list[OutlierFlag] = []
    for role, role_records in sorted(grouped.items()):
        by_level: dict[float, list[AnalysisReading]] = defaultdict(list)
        for record in role_records:
            by_level[record.concentration].append(record)
        for level, level_records in sorted(by_level.items()):
            values = [
                (item.measurement_id, float(item.response))
                for item in level_records
                if item.response is not None
            ]
            outlier_flags.extend(
                flag_modified_z_score_outliers(
                    values,
                    group=f"{role}:{level:g}",
                    threshold=policy.outlier_modified_z_threshold,
                )
            )
    outliers_ok = not outlier_flags or not policy.fail_on_flagged_outlier
    checks.append(
        QCCheck(
            code="outlier_review",
            passed=outliers_ok,
            message=(
                "no outliers flagged"
                if not outlier_flags
                else "outliers flagged and retained for review"
                if outliers_ok
                else "flagged outliers violate integrity policy"
            ),
            measurement_ids=tuple(flag.measurement_id for flag in outlier_flags),
            details={
                "method": "modified_z_score_mad",
                "threshold": policy.outlier_modified_z_threshold,
                "excluded_from_metrics": False,
            },
        )
    )
    passed = all(check.passed for check in checks)
    return IntegrityQCReport(
        passed=passed,
        optimizer_eligible=passed,
        checks=tuple(checks),
        outlier_flags=tuple(outlier_flags),
    )
