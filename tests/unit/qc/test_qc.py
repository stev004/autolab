from __future__ import annotations

import pytest

from autonomous_lab.analysis.metrics import analyze_simulated_batch, readings_from_simulation
from autonomous_lab.qc.checks import MetricConstraint, evaluate_performance_constraints
from autonomous_lab.qc.engine import IntegrityQCConfig, evaluate_integrity_qc
from autonomous_lab.simulation.assay_model import (
    ExecutedAssayConditions,
    FailureInjection,
    NonlinearAssaySimulator,
    SimulationConfig,
)


def _batch(failures: FailureInjection | None = None):
    simulator = NonlinearAssaySimulator(
        SimulationConfig(outlier_probability=0, gaussian_noise_sd=0.002, batch_drift_sd=0.001)
    )
    return simulator.simulate_batch(
        experiment_id="qc-1",
        experiment_ordinal=1,
        campaign_seed=73109,
        conditions=ExecutedAssayConditions(0.55, 100, 55, 15, 29.5),
        failures=failures,
    )


def test_nominal_batch_passes_integrity_qc() -> None:
    report = evaluate_integrity_qc(readings_from_simulation(_batch()))

    assert report.passed
    assert report.optimizer_eligible
    assert report.failed_check_codes == ()


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (FailureInjection(missing_well_id="CTL-01"), "measurements_complete"),
        (FailureInjection(missing_well_id="CHK-01"), "check_standard_replicates"),
        (FailureInjection(contaminated_blank=True), "blank_contamination"),
        (FailureInjection(excessive_drift=True), "blank_drift"),
    ],
)
def test_integrity_faults_are_qc_failed_and_optimizer_ineligible(
    failure: FailureInjection, expected_code: str
) -> None:
    report = evaluate_integrity_qc(readings_from_simulation(_batch(failure)))

    assert not report.passed
    assert not report.optimizer_eligible
    assert expected_code in report.failed_check_codes


def test_outliers_are_flagged_but_retained_by_default() -> None:
    batch = _batch(FailureInjection(single_outlier_well_id="CTL-02"))
    report = evaluate_integrity_qc(readings_from_simulation(batch))
    analysis = analyze_simulated_batch(batch)

    assert any(flag.measurement_id.endswith("CTL-02:absorbance") for flag in report.outlier_flags)
    assert all(not flag.excluded_from_metrics for flag in report.outlier_flags)
    assert any(
        identifier.endswith("CTL-02:absorbance") for identifier in analysis.included_measurement_ids
    )


def test_outlier_policy_can_require_review_without_removing_reading() -> None:
    batch = _batch(FailureInjection(single_outlier_well_id="CTL-02"))
    report = evaluate_integrity_qc(
        readings_from_simulation(batch), IntegrityQCConfig(fail_on_flagged_outlier=True)
    )

    assert not report.passed
    assert "outlier_review" in report.failed_check_codes
    assert all(not flag.excluded_from_metrics for flag in report.outlier_flags)


def test_scientific_performance_failure_is_not_integrity_failure() -> None:
    batch = _batch()
    integrity = evaluate_integrity_qc(readings_from_simulation(batch))
    metrics = analyze_simulated_batch(batch).metric_values()
    evaluations = evaluate_performance_constraints(
        metrics,
        (
            MetricConstraint("control_cv", "lte", 0.000001),
            MetricConstraint("calibration_r_squared", "gte", 0.9999999),
            MetricConstraint("signal_to_noise", "gte", 1_000_000),
        ),
    )

    assert integrity.passed
    assert any(not item.feasible for item in evaluations)
    assert all(item.residual > 0 for item in evaluations if not item.feasible)


def test_performance_constraint_residual_is_nonpositive_when_feasible() -> None:
    evaluations = evaluate_performance_constraints(
        {"cv": 0.04, "snr": 12.0},
        (MetricConstraint("cv", "lte", 0.05), MetricConstraint("snr", "gte", 10.0)),
    )

    assert [item.residual for item in evaluations] == pytest.approx([-0.01, -2.0])
    assert all(item.feasible for item in evaluations)
