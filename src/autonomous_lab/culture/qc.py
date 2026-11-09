"""Run-integrity QC for culture experiments.

QC answers "can we trust this run enough to learn from it?", not "was it a good
culture?". A trustworthy run with poor growth is still a valid observation; a run
whose heater drifted or whose protection tripped is not, because the conditions
the optimiser asked for were not the conditions the culture experienced.

Thresholds come from ``CultureQCConfig`` and are illustrative defaults.
"""

from __future__ import annotations

import itertools
import math
from decimal import Decimal
from typing import Any

from pydantic import Field

from autonomous_lab.culture.controller import EnvironmentTelemetry
from autonomous_lab.culture.evidence import CultureRunEvidence, ReplicateEvidence
from autonomous_lab.culture.models import (
    LIGHT_UNIT,
    TEMPERATURE_UNIT,
    CultureQCConfig,
    seconds,
    to_timedelta,
)
from autonomous_lab.culture.timeseries import TimeSeriesReconciliation
from autonomous_lab.domain.base import DomainModel
from autonomous_lab.hardware.sensors.calibration import CalibrationStatus

QC_VERSION = "culture_integrity_qc@2"


class CultureQCCheck(DomainModel):
    code: str
    passed: bool
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class CultureQCReport(DomainModel):
    """QC for one biological replicate (one reactor)."""

    qc_version: str = QC_VERSION
    reactor_id: str
    passed: bool
    checks: tuple[CultureQCCheck, ...]

    @property
    def failed_codes(self) -> tuple[str, ...]:
        return tuple(check.code for check in self.checks if not check.passed)


class CultureExperimentQC(DomainModel):
    """Experiment-level verdict: enough replicates passed to learn from their mean."""

    qc_version: str = QC_VERSION
    passed: bool
    minimum_passing_replicates: int
    passing_reactors: tuple[str, ...]
    replicates: tuple[CultureQCReport, ...]

    @property
    def failed_codes(self) -> tuple[str, ...]:
        """Distinct failing check codes across replicates, in first-seen order."""

        codes: list[str] = []
        for replicate in self.replicates:
            codes += [code for code in replicate.failed_codes if code not in codes]
        if not self.passed and not codes:
            codes.append("too_few_passing_replicates")
        return tuple(codes)


def evaluate_culture_qc(
    evidence: CultureRunEvidence,
    reconciliations: dict[str, TimeSeriesReconciliation],
    config: CultureQCConfig,
    *,
    minimum_passing_replicates: int,
) -> CultureExperimentQC:
    reports = tuple(
        evaluate_replicate_qc(evidence, replicate, reconciliations[replicate.reactor_id], config)
        for replicate in evidence.replicates
    )
    passing = tuple(report.reactor_id for report in reports if report.passed)
    return CultureExperimentQC(
        passed=len(passing) >= minimum_passing_replicates,
        minimum_passing_replicates=minimum_passing_replicates,
        passing_reactors=passing,
        replicates=reports,
    )


def evaluate_replicate_qc(
    run: CultureRunEvidence,
    replicate: ReplicateEvidence,
    reconciliation: TimeSeriesReconciliation,
    config: CultureQCConfig,
) -> CultureQCReport:
    checks = (
        _setpoint_acknowledged(replicate),
        _hold_continuous(replicate),
        _no_protection_trip(replicate),
        _telemetry_coverage(run, replicate, config),
        _temperature_control(run, replicate, config),
        _readings_complete(reconciliation, config),
        _no_unexpected_readings(reconciliation),
        _no_saturation(run, reconciliation),
        _calibration_valid(run, reconciliation),
        _no_contamination_indicator(replicate, reconciliation, config),
        _growth_plausible(reconciliation, config),
    )
    return CultureQCReport(
        reactor_id=replicate.reactor_id,
        passed=all(check.passed for check in checks),
        checks=checks,
    )


def _check(code: str, passed: bool, ok: str, failed: str, **details: Any) -> CultureQCCheck:
    return CultureQCCheck(
        code=code, passed=passed, message=ok if passed else failed, details=details
    )


def _setpoint_acknowledged(evidence: ReplicateEvidence) -> CultureQCCheck:
    ack = evidence.acknowledgement
    command = evidence.setpoint_command
    applied_matches = (
        ack is not None
        and ack.accepted
        and ack.command_id == command.command_id
        and ack.applied_temperature_setpoint is not None
        and ack.applied_light_intensity is not None
        and ack.applied_temperature_setpoint.to(TEMPERATURE_UNIT).value
        == command.temperature_setpoint.to(TEMPERATURE_UNIT).value
        and ack.applied_light_intensity.to(LIGHT_UNIT).value
        == command.light_intensity.to(LIGHT_UNIT).value
    )
    return _check(
        "setpoint_acknowledged",
        applied_matches,
        "device applied exactly the requested setpoints",
        "device did not acknowledge applying the requested setpoints",
        acknowledgement=None if ack is None else ack.model_dump(mode="json"),
    )


def _no_protection_trip(evidence: ReplicateEvidence) -> CultureQCCheck:
    tripped = [
        sample.sampled_at.isoformat()
        for sample in evidence.telemetry
        if sample.protection_tripped or sample.trip_count > 0
    ]
    return _check(
        "no_protection_trip",
        not tripped,
        "device protection never tripped",
        "device protection tripped during the hold",
        tripped_at=tripped,
    )


def _post_warmup(
    run: CultureRunEvidence, evidence: ReplicateEvidence, config: CultureQCConfig
) -> list[EnvironmentTelemetry]:
    settled = run.hold_started_at + to_timedelta(config.warmup)
    return [sample for sample in evidence.telemetry if sample.sampled_at >= settled]


def _hold_continuous(evidence: ReplicateEvidence) -> CultureQCCheck:
    return _check(
        "hold_continuous",
        not evidence.hold_interrupted,
        "the device held the requested conditions throughout",
        "AutoLab could not confirm the device kept holding across a restart",
    )


def _telemetry_coverage(
    run: CultureRunEvidence, evidence: ReplicateEvidence, config: CultureQCConfig
) -> CultureQCCheck:
    """Coverage against the *expected* sample count, so outages count as gaps."""

    window = _post_warmup(run, evidence, config)
    settled = run.hold_started_at + to_timedelta(config.warmup)
    span = (run.ended_at - settled).total_seconds()
    interval = float(seconds(run.receipt.protocol.telemetry_interval))
    expected = max(int(span // interval) + 1, 1) if span > 0 else 1
    with_value = [sample for sample in window if sample.measured_temperature is not None]
    coverage = min(Decimal(len(with_value)) / Decimal(expected), Decimal(1))
    return _check(
        "telemetry_coverage",
        coverage >= config.minimum_telemetry_coverage,
        "enough temperature telemetry after warm-up",
        "too many missing temperature readings after warm-up",
        coverage=str(coverage),
        samples=len(window),
        expected_samples=expected,
    )


def _temperature_control(
    run: CultureRunEvidence, evidence: ReplicateEvidence, config: CultureQCConfig
) -> CultureQCCheck:
    setpoint = evidence.setpoint_command.temperature_setpoint.to(TEMPERATURE_UNIT).value
    values = [
        sample.measured_temperature.to(TEMPERATURE_UNIT).value
        for sample in _post_warmup(run, evidence, config)
        if sample.measured_temperature is not None
    ]
    if not values:
        return _check(
            "temperature_control", False, "", "no post-warm-up temperature telemetry to check"
        )
    deviation = sum((abs(value - setpoint) for value in values), Decimal(0)) / len(values)
    return _check(
        "temperature_control",
        deviation <= config.maximum_mean_temperature_deviation,
        "measured temperature tracked the setpoint",
        "measured temperature drifted from the setpoint",
        mean_absolute_deviation=str(deviation),
        limit=str(config.maximum_mean_temperature_deviation),
    )


def _readings_complete(
    reconciliation: TimeSeriesReconciliation, config: CultureQCConfig
) -> CultureQCCheck:
    entries = reconciliation.entries
    not_matched = [entry.expected_measurement_id for entry in entries if entry.status != "matched"]
    fraction = Decimal(len(not_matched)) / Decimal(len(entries))
    final_ok = (not config.require_final_reading) or entries[-1].status == "matched"
    return _check(
        "readings_complete",
        fraction <= config.maximum_missing_reading_fraction and final_ok,
        "sampling plan satisfied",
        "too many missing or out-of-window readings, or the final reading is missing",
        not_matched=not_matched,
        missing_fraction=str(fraction),
        final_reading_matched=entries[-1].status == "matched",
    )


def _no_unexpected_readings(reconciliation: TimeSeriesReconciliation) -> CultureQCCheck:
    extra = [str(item) for item in reconciliation.unexpected_measurement_ids]
    return _check(
        "no_unexpected_readings",
        not extra,
        "every reading belongs to exactly one plan entry",
        "readings were received that the plan did not expect",
        unexpected=extra,
    )


def _no_saturation(
    run: CultureRunEvidence, reconciliation: TimeSeriesReconciliation
) -> CultureQCCheck:
    capabilities = run.sensor_capabilities
    saturated: list[str] = []
    for entry in reconciliation.entries:
        measurement = entry.measurement
        if measurement is None:
            continue
        span = capabilities.measurement_ranges.get(measurement.measurement_type)
        at_limit = False
        if span is not None:
            low = span.minimum.to(measurement.unit).value
            high = span.maximum.to(measurement.unit).value
            at_limit = not low < measurement.raw_value < high
        if measurement.metadata.get("saturated") is True or at_limit:
            saturated.append(entry.expected_measurement_id)
    return _check(
        "no_sensor_saturation",
        not saturated,
        "no reading at or beyond the sensor's measurement range",
        "sensor saturated: readings at the range limit carry no information",
        saturated=saturated,
    )


def _calibration_valid(
    run: CultureRunEvidence, reconciliation: TimeSeriesReconciliation
) -> CultureQCCheck:
    # Every calibration the sensor reported across sessions, by its hash.
    calibrations = {
        (item.measurement_type, item.calibration_sha256): item
        for session in run.sensor_sessions
        for item in session.calibrations
    }
    invalid: list[str] = []
    for entry in reconciliation.entries:
        measurement = entry.measurement
        if measurement is None:
            continue
        calibration = calibrations.get(
            (measurement.measurement_type, measurement.source_evidence.calibration_sha256 or "")
        )
        if calibration is None:
            if run.sensor_capabilities.calibration_required:
                invalid.append(entry.expected_measurement_id)
            continue
        expired = (
            calibration.expires_at is not None and measurement.measured_at >= calibration.expires_at
        )
        if (
            calibration.status is not CalibrationStatus.VALID
            or measurement.measured_at < calibration.calibrated_at
            or expired
            or measurement.source_evidence.calibration_sha256 != calibration.calibration_sha256
        ):
            invalid.append(entry.expected_measurement_id)
    return _check(
        "calibration_valid",
        not invalid,
        "every reading was taken under a valid calibration",
        "some readings were taken outside a valid sensor calibration",
        invalid=invalid,
    )


def _no_contamination_indicator(
    evidence: ReplicateEvidence,
    reconciliation: TimeSeriesReconciliation,
    config: CultureQCConfig,
) -> CultureQCCheck:
    """Device faults or sensor metadata that report suspected contamination."""

    flagged = sorted(
        {
            fault
            for sample in evidence.telemetry
            for fault in sample.faults
            if fault in config.contamination_fault_codes
        }
        | {
            entry.expected_measurement_id
            for entry in reconciliation.entries
            if entry.measurement is not None
            and entry.measurement.metadata.get("contamination_suspected") is True
        }
    )
    return _check(
        "no_contamination_indicator",
        not flagged,
        "no device or sensor contamination indicator",
        "device or sensor reported suspected contamination",
        indicators=flagged,
    )


def _growth_plausible(
    reconciliation: TimeSeriesReconciliation,
    config: CultureQCConfig,
) -> CultureQCCheck:
    """Flag OD drops and implausibly fast growth between consecutive readings.

    Both are common signatures of contamination, lysis or probe fouling. The
    limits are illustrative; this check cannot tell those causes apart.
    """

    points = [
        (entry.measurement.measured_at, entry.measurement.raw_value, entry.expected_measurement_id)
        for entry in reconciliation.matched()
        if entry.measurement is not None
    ]
    drops: list[str] = []
    too_fast: list[str] = []
    for (t0, od0, _), (t1, od1, later_id) in itertools.pairwise(points):
        if config.maximum_od_drop is not None and od0 - od1 > config.maximum_od_drop:
            drops.append(later_id)
        hours = Decimal(str((t1 - t0).total_seconds())) / Decimal(3600)
        ceiling = config.maximum_specific_growth_rate_per_hour
        if ceiling is not None and od0 > 0 and od1 > 0 and hours > 0:
            rate = Decimal(str(math.log(float(od1 / od0)))) / hours
            if rate > ceiling:
                too_fast.append(later_id)
    return _check(
        "growth_plausible",
        not drops and not too_fast,
        "OD series shows no drops or implausibly fast growth",
        "OD series looks implausible (possible contamination, lysis or probe fouling)",
        od_drops_at=drops,
        too_fast_at=too_fast,
        maximum_od_drop=None if config.maximum_od_drop is None else str(config.maximum_od_drop),
        maximum_specific_growth_rate_per_hour=None
        if config.maximum_specific_growth_rate_per_hour is None
        else str(config.maximum_specific_growth_rate_per_hour),
    )
