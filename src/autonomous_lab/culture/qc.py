"""Run-integrity QC for culture experiments.

QC answers "can we trust this run enough to learn from it?", not "was it a good
culture?". A trustworthy run with poor growth is still a valid observation; a run
whose heater drifted or whose protection tripped is not, because the conditions
the optimiser asked for were not the conditions the culture experienced.

Thresholds come from ``CultureQCConfig`` and are illustrative defaults.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import Field

from autonomous_lab.culture.controller import EnvironmentTelemetry
from autonomous_lab.culture.evidence import CultureRunEvidence
from autonomous_lab.culture.models import (
    LIGHT_UNIT,
    TEMPERATURE_UNIT,
    CultureQCConfig,
    to_timedelta,
)
from autonomous_lab.culture.timeseries import TimeSeriesReconciliation
from autonomous_lab.domain.base import DomainModel
from autonomous_lab.hardware.sensors.calibration import CalibrationStatus

QC_VERSION = "culture_integrity_qc@1"


class CultureQCCheck(DomainModel):
    code: str
    passed: bool
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class CultureQCReport(DomainModel):
    qc_version: str = QC_VERSION
    passed: bool
    checks: tuple[CultureQCCheck, ...]

    @property
    def failed_codes(self) -> tuple[str, ...]:
        return tuple(check.code for check in self.checks if not check.passed)


def evaluate_culture_qc(
    evidence: CultureRunEvidence,
    reconciliation: TimeSeriesReconciliation,
    config: CultureQCConfig,
) -> CultureQCReport:
    checks = (
        _setpoint_acknowledged(evidence),
        _no_protection_trip(evidence),
        _telemetry_coverage(evidence, config),
        _temperature_control(evidence, config),
        _readings_complete(reconciliation, config),
        _no_unexpected_readings(reconciliation),
        _no_saturation(evidence, reconciliation),
        _calibration_valid(evidence, reconciliation),
    )
    return CultureQCReport(passed=all(check.passed for check in checks), checks=checks)


def _check(code: str, passed: bool, ok: str, failed: str, **details: Any) -> CultureQCCheck:
    return CultureQCCheck(
        code=code, passed=passed, message=ok if passed else failed, details=details
    )


def _setpoint_acknowledged(evidence: CultureRunEvidence) -> CultureQCCheck:
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


def _no_protection_trip(evidence: CultureRunEvidence) -> CultureQCCheck:
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
    evidence: CultureRunEvidence, config: CultureQCConfig
) -> list[EnvironmentTelemetry]:
    settled = evidence.hold_started_at + to_timedelta(config.warmup)
    return [sample for sample in evidence.telemetry if sample.sampled_at >= settled]


def _telemetry_coverage(evidence: CultureRunEvidence, config: CultureQCConfig) -> CultureQCCheck:
    window = _post_warmup(evidence, config)
    with_value = [sample for sample in window if sample.measured_temperature is not None]
    coverage = Decimal(len(with_value)) / Decimal(len(window)) if window else Decimal(0)
    return _check(
        "telemetry_coverage",
        coverage >= config.minimum_telemetry_coverage,
        "enough temperature telemetry after warm-up",
        "too many missing temperature readings after warm-up",
        coverage=str(coverage),
        samples=len(window),
    )


def _temperature_control(evidence: CultureRunEvidence, config: CultureQCConfig) -> CultureQCCheck:
    setpoint = evidence.setpoint_command.temperature_setpoint.to(TEMPERATURE_UNIT).value
    values = [
        sample.measured_temperature.to(TEMPERATURE_UNIT).value
        for sample in _post_warmup(evidence, config)
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
    evidence: CultureRunEvidence, reconciliation: TimeSeriesReconciliation
) -> CultureQCCheck:
    capabilities = evidence.sensor_capabilities
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
    evidence: CultureRunEvidence, reconciliation: TimeSeriesReconciliation
) -> CultureQCCheck:
    calibrations = {
        item.measurement_type: item for item in evidence.sensor_capabilities.calibrations
    }
    invalid: list[str] = []
    for entry in reconciliation.entries:
        measurement = entry.measurement
        if measurement is None:
            continue
        calibration = calibrations.get(measurement.measurement_type)
        if calibration is None:
            if evidence.sensor_capabilities.calibration_required:
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
