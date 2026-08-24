from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from autonomous_lab.analysis.domain_adapters import (
    derived_metrics_from_analysis,
    readings_from_domain,
)
from autonomous_lab.analysis.metrics import analyze_assay
from autonomous_lab.qc.domain_adapters import qc_result_from_integrity_report
from autonomous_lab.qc.engine import evaluate_integrity_qc
from autonomous_lab.simulation.assay_model import (
    ExecutedAssayConditions,
    FailureInjection,
    NonlinearAssaySimulator,
    SimulationConfig,
)
from autonomous_lab.simulation.domain_adapters import measurements_from_simulated_batch


def _batch(failure: FailureInjection | None = None):
    return NonlinearAssaySimulator(SimulationConfig(outlier_probability=0)).simulate_batch(
        experiment_id="external-experiment-id",
        experiment_ordinal=1,
        campaign_seed=5,
        conditions=ExecutedAssayConditions(0.55, 100, 55, 15, 29.5),
        failures=failure,
    )


def test_complete_simulator_batch_round_trips_through_domain_measurements() -> None:
    experiment_id = uuid4()
    run_id = uuid4()
    batch = _batch()
    domain_measurements = measurements_from_simulated_batch(
        batch,
        experiment_id=experiment_id,
        run_id=run_id,
        measured_at=datetime(2026, 8, 14, tzinfo=UTC),
    )
    readings = readings_from_domain(domain_measurements)
    analysis = analyze_assay(
        readings,
        cost_per_test=batch.cost_per_test,
        reagent_consumption_mg_per_test=batch.reagent_amount_mg_per_test,
        reagent_volume_microliter_per_test=batch.reagent_volume_microliter_per_test,
    )
    derived = derived_metrics_from_analysis(analysis, experiment_id=experiment_id)
    qc_report = evaluate_integrity_qc(readings)
    qc_result = qc_result_from_integrity_report(
        qc_report, experiment_id=experiment_id, run_id=run_id
    )

    assert len(domain_measurements) == len(batch.measurements)
    assert all(item.experiment_id == experiment_id for item in domain_measurements)
    assert domain_measurements[0].metadata["simulator_config_hash"] == batch.simulator_config_hash
    assert all(item.experiment_id == experiment_id for item in derived)
    cost = next(item for item in derived if item.name == "cost_per_test")
    assert cost.source_measurement_ids == ()
    assert cost.unit == "GBP"
    reagent = next(item for item in derived if item.name == "reagent_consumption")
    assert reagent.unit == "milligram"
    signal = next(item for item in derived if item.name == "signal_to_noise")
    assert len(signal.source_measurement_ids) == len(batch.measurements)
    assert qc_result.status.value == "passed"


def test_missing_simulator_reading_cannot_masquerade_as_domain_measurement() -> None:
    batch = _batch(FailureInjection(missing_well_id="CTL-01"))

    with pytest.raises(ValueError, match="missing"):
        measurements_from_simulated_batch(
            batch,
            experiment_id=uuid4(),
            run_id=uuid4(),
            measured_at=datetime(2026, 8, 14, tzinfo=UTC),
        )


def test_measurement_ids_are_deterministic_within_run_and_distinct_across_runs() -> None:
    batch = _batch()
    experiment_id = uuid4()
    first_run_id = uuid4()
    second_run_id = uuid4()
    measured_at = datetime(2026, 8, 14, tzinfo=UTC)

    first = measurements_from_simulated_batch(
        batch,
        experiment_id=experiment_id,
        run_id=first_run_id,
        measured_at=measured_at,
    )
    replay = measurements_from_simulated_batch(
        batch,
        experiment_id=experiment_id,
        run_id=first_run_id,
        measured_at=measured_at,
    )
    second = measurements_from_simulated_batch(
        batch,
        experiment_id=experiment_id,
        run_id=second_run_id,
        measured_at=measured_at,
    )

    assert [item.measurement_id for item in first] == [item.measurement_id for item in replay]
    assert {item.measurement_id for item in first}.isdisjoint(
        item.measurement_id for item in second
    )


def test_qc_adapter_accepts_explicit_mapping_for_neutral_raw_ids() -> None:
    batch = _batch(FailureInjection(single_outlier_well_id="CTL-02"))
    experiment_id = uuid4()
    run_id = uuid4()
    measurements = measurements_from_simulated_batch(
        batch,
        experiment_id=experiment_id,
        run_id=run_id,
        measured_at=datetime(2026, 8, 14, tzinfo=UTC),
    )
    identifier_map = {
        raw.measurement_id: domain.measurement_id
        for raw, domain in zip(batch.measurements, measurements, strict=True)
    }
    report = evaluate_integrity_qc(
        tuple(
            # Analysis IDs are deliberately changed back to neutral simulator IDs
            # to prove the adapter's explicit identifier mapping behavior.
            reading.__class__(
                measurement_id=raw.measurement_id,
                role=reading.role,
                concentration=reading.concentration,
                response=reading.response,
                acquisition_index=reading.acquisition_index,
                status=reading.status,
            )
            for raw, reading in zip(
                batch.measurements, readings_from_domain(measurements), strict=True
            )
        )
    )

    result = qc_result_from_integrity_report(
        report,
        experiment_id=experiment_id,
        run_id=run_id,
        measurement_id_map=identifier_map,
    )

    assert result.outlier_assessment is not None
    assert result.outlier_assessment.flagged_measurement_ids
    assert not result.outlier_assessment.metrics_recomputed_with_exclusions
