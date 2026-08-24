from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from autonomous_lab.storage import (
    ArtifactStore,
    Database,
    ImmutableRecordError,
    StateConflictError,
    UnitOfWork,
    sqlite_pragmas,
)
from autonomous_lab.storage.orm import (
    AuditEventRecord,
    MeasurementRecord,
    OptimizerObservationRecord,
)


@pytest.fixture
def database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "autolab.sqlite3")
    database.create_schema()
    try:
        yield database
    finally:
        database.dispose()


def _add_campaign_and_experiment(database: Database) -> tuple[str, str]:
    campaign_id = "00000000-0000-0000-0000-000000000001"
    experiment_id = "00000000-0000-0000-0000-000000000002"
    with UnitOfWork(database) as uow:
        uow.campaigns.add(
            campaign_id=campaign_id,
            name="assay",
            state="DRAFT",
            schema_version="1",
            config_source="schema_version: '1'",
            config={"schema_version": "1"},
            objective={"minimize": ["cost_per_test", "reagent_consumption"]},
            parameter_space={"reagent_volume": {"minimum": 20, "maximum": 200}},
            application_version="0.1.0",
            git_commit="abc123",
            python_version="3.12",
            platform="test",
            uv_lock_sha256="a" * 64,
        )
        uow.campaigns.transition(campaign_id, "READY", expected_state="DRAFT")
        uow.experiments.add(
            experiment_id=experiment_id,
            campaign_id=campaign_id,
            ordinal=0,
            state="PROPOSED",
            optimizer_trial_id="trial-0",
        )
        uow.commit()
    return campaign_id, experiment_id


def test_full_scientific_record_is_reconstructable(database: Database) -> None:
    campaign_id, experiment_id = _add_campaign_and_experiment(database)
    measured_at = datetime(2026, 8, 14, 10, 0, tzinfo=UTC)

    with UnitOfWork(database) as uow:
        proposal = uow.records.add_proposal(
            proposal_id="00000000-0000-0000-0000-000000000003",
            experiment_id=experiment_id,
            schema_version="1",
            proposal={"reagent_volume": {"value": 50, "unit": "microliter"}},
            strategy_name="random",
            strategy_version="1",
            strategy_trial_id="trial-0",
        )
        protocol = uow.records.add_protocol(
            protocol_id="00000000-0000-0000-0000-000000000004",
            experiment_id=experiment_id,
            schema_version="1",
            protocol={"steps": [{"command_type": "dispense"}]},
            compiler_name="assay",
            compiler_version="1",
        )
        validation = uow.records.add_validation_report(
            report_id="00000000-0000-0000-0000-000000000005",
            protocol_id=protocol.id,
            status="PASSED",
            protocol_hash=protocol.protocol_hash,
            capability_snapshot={"commands": ["dispense", "measure_absorbance"]},
            validation_policy={"maximum_well_volume": 300},
            validator_version="1",
            issues=[
                {
                    "code": "LOW_HEADROOM",
                    "severity": "warning",
                    "path": "steps.0",
                    "message": "limited headroom",
                    "rule_name": "vessel_capacity",
                    "rule_version": "1",
                }
            ],
        )
        run = uow.records.begin_execution(
            run_id="00000000-0000-0000-0000-000000000006",
            experiment_id=experiment_id,
            protocol_id=protocol.id,
            validation_report_id=validation.id,
            backend_name="simulated",
            backend_version="1",
            device_snapshot={"spectrophotometer": "sim-1"},
        )
        started = uow.records.append_action_event(
            run_id=run.id,
            sequence=0,
            step_id="step-1",
            event_type="STARTED",
            status="RUNNING",
            idempotency_key="run:step-1",
            retry_safe=True,
            command={"command_type": "measure_absorbance"},
            started_at=measured_at,
        )
        uow.records.append_action_event(
            run_id=run.id,
            sequence=1,
            step_id="step-1",
            event_type="COMPLETED",
            status="SUCCEEDED",
            idempotency_key="run:step-1",
            retry_safe=True,
            command={"command_type": "measure_absorbance"},
            response={"absorbance": 0.5},
            completed_at=measured_at,
        )
        measurement = uow.records.add_measurement(
            measurement_id="00000000-0000-0000-0000-000000000007",
            experiment_id=experiment_id,
            run_id=run.id,
            action_event_id=started.id,
            measurement_type="absorbance",
            sample_role="control",
            well_id="A1",
            analyte_level=0.5,
            replicate_index=0,
            value=0.5,
            unit="absorbance_unit",
            raw_payload={"adc": 1234},
            provenance={"device_id": "sim-spec", "stream": "well:A1"},
            measured_at=measured_at,
        )
        uow.records.finish_execution(run.id, status="SUCCEEDED", safe_state_reached=True)
        qc_run = uow.records.add_qc_run(
            qc_run_id="00000000-0000-0000-0000-000000000008",
            experiment_id=experiment_id,
            run_id=run.id,
            status="PASSED",
            eligible_for_optimization=True,
            algorithm_name="assay-integrity",
            algorithm_version="1",
            checks=[
                {
                    "name": "required_measurements",
                    "status": "PASSED",
                    "method": "count",
                    "observed": {"count": 1},
                }
            ],
        )
        uow.records.add_metric(
            experiment_id=experiment_id,
            qc_run_id=qc_run.id,
            name="cost_per_test",
            value=0.12,
            unit="GBP",
            algorithm_name="executed-cost",
            algorithm_version="1",
            source_measurement_ids=[measurement.id],
        )
        uow.records.add_constraint_result(
            experiment_id=experiment_id,
            metric_name="control_cv",
            operator="lte",
            threshold=0.05,
            actual=0.03,
            residual=-0.02,
            feasible=True,
        )
        optimizer = uow.optimization.add_run(
            optimizer_run_id="00000000-0000-0000-0000-000000000009",
            campaign_id=campaign_id,
            strategy_name="random",
            strategy_version="1",
            seed=7,
            config={"seed": 7},
        )
        uow.optimization.add_observation(
            optimizer_run_id=optimizer.id,
            experiment_id=experiment_id,
            trial_id="trial-0",
            objective_values={"cost_per_test": 0.12},
            constraint_residuals={"control_cv": -0.02},
            qc_eligible=True,
            feasible=True,
            observation={"schema_version": "1"},
        )
        uow.optimization.add_checkpoint(
            optimizer_run_id=optimizer.id,
            ordinal=1,
            state={"observed_trial_ids": ["trial-0"]},
        )
        uow.experiments.transition(experiment_id, "OBSERVED", expected_state="PROPOSED")
        uow.commit()

    with UnitOfWork(database) as uow:
        evidence = uow.records.experiment_evidence(experiment_id)
        summary = uow.campaigns.summary(campaign_id)
        assert proposal.sha256
        assert evidence["measurements"][0].raw_payload == {"adc": 1234}
        assert evidence["metrics"][0].name == "cost_per_test"
        assert evidence["measurements"][0].id != evidence["metrics"][0].id
        assert summary.experiment_count == 1
        assert summary.qc_passed_count == 1
        assert summary.observed_count == 1
        assert summary.feasible_count == 1


def test_measurements_are_append_only(database: Database) -> None:
    _, experiment_id = _add_campaign_and_experiment(database)
    with UnitOfWork(database) as uow:
        protocol = uow.records.add_protocol(
            protocol_id="00000000-0000-0000-0000-000000000010",
            experiment_id=experiment_id,
            schema_version="1",
            protocol={"steps": []},
            compiler_name="test",
            compiler_version="1",
        )
        validation = uow.records.add_validation_report(
            report_id="00000000-0000-0000-0000-000000000011",
            protocol_id=protocol.id,
            status="PASSED",
            protocol_hash=protocol.protocol_hash,
            capability_snapshot={},
            validation_policy={},
            validator_version="1",
        )
        run = uow.records.begin_execution(
            run_id="00000000-0000-0000-0000-000000000012",
            experiment_id=experiment_id,
            protocol_id=protocol.id,
            validation_report_id=validation.id,
            backend_name="simulated",
        )
        uow.records.add_measurement(
            measurement_id="00000000-0000-0000-0000-000000000013",
            experiment_id=experiment_id,
            run_id=run.id,
            measurement_type="absorbance",
            sample_role="blank",
            value=0.01,
            unit="absorbance_unit",
            raw_payload={},
            provenance={},
            measured_at=datetime.now(UTC),
        )
        uow.commit()

    with pytest.raises(ImmutableRecordError), UnitOfWork(database) as uow:
        assert uow.session is not None
        record = uow.session.get(MeasurementRecord, "00000000-0000-0000-0000-000000000013")
        assert record is not None
        record.value = 999
        uow.flush()


def test_state_guards_and_audit_are_atomic(database: Database) -> None:
    campaign_id, _ = _add_campaign_and_experiment(database)
    with pytest.raises(StateConflictError), UnitOfWork(database) as uow:
        uow.campaigns.transition(campaign_id, "RUNNING", expected_state="DRAFT")
        uow.commit()

    with UnitOfWork(database) as uow:
        assert uow.session is not None
        events = list(
            uow.session.scalars(
                select(AuditEventRecord)
                .where(AuditEventRecord.campaign_id == campaign_id)
                .order_by(AuditEventRecord.sequence)
            )
        )
        assert [event.event_type for event in events[:2]] == [
            "campaign.created",
            "campaign.state_changed",
        ]


def test_optimizer_observation_is_exactly_once(database: Database) -> None:
    campaign_id, experiment_id = _add_campaign_and_experiment(database)
    with UnitOfWork(database) as uow:
        optimizer = uow.optimization.add_run(
            optimizer_run_id="00000000-0000-0000-0000-000000000020",
            campaign_id=campaign_id,
            strategy_name="random",
            strategy_version="1",
            seed=1,
            config={},
        )
        for observation_id in ("first", "second"):
            uow.optimization.add_observation(
                observation_id=observation_id,
                optimizer_run_id=optimizer.id,
                experiment_id=experiment_id,
                trial_id="trial-0",
                objective_values={},
                constraint_residuals={},
                qc_eligible=True,
                feasible=False,
                observation={},
            )
        with pytest.raises(IntegrityError):
            uow.flush()

    with UnitOfWork(database) as uow:
        assert uow.session is not None
        assert uow.session.scalar(select(OptimizerObservationRecord.id)) is None


def test_sqlite_pragmas_foreign_keys_and_wal(database: Database) -> None:
    pragmas = sqlite_pragmas(database.engine)
    assert pragmas["foreign_keys"] == 1
    assert pragmas["journal_mode"] == "wal"
    assert pragmas["busy_timeout"] == 5000

    with pytest.raises(IntegrityError), UnitOfWork(database) as uow:
        uow.experiments.add(
            experiment_id="orphan",
            campaign_id="missing",
            ordinal=0,
            state="PROPOSED",
        )
        uow.commit()


def test_alembic_initial_migration_creates_full_schema(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "migrated.sqlite3"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{sqlite_path}")
    command.upgrade(config, "head")

    database = Database(sqlite_path)
    try:
        tables = set(inspect(database.engine).get_table_names())
        assert {
            "campaigns",
            "experiments",
            "experiment_results",
            "proposals",
            "protocols",
            "validation_reports",
            "execution_runs",
            "action_events",
            "measurements",
            "expected_measurement_plans",
            "measurement_ingestion_batches",
            "external_measurement_revisions",
            "qc_runs",
            "derived_metrics",
            "optimizer_observations",
            "optimizer_checkpoints",
            "artifacts",
            "audit_events",
        } <= tables
        assert "alembic_version" in tables
        with database.engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == "0005"
            )
        campaign_columns = {
            item["name"] for item in inspect(database.engine).get_columns("campaigns")
        }
        assert {
            "config_source_sha256",
            "config_sha256",
            "git_dirty",
            "source_tree_diff_sha256",
        } <= campaign_columns
    finally:
        database.dispose()


def test_artifact_store_is_content_addressed_and_rejects_unsafe_suffix(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    first = store.put_bytes(b"raw evidence", media_type="text/plain", suffix=".txt")
    second = store.put_bytes(b"raw evidence", media_type="text/plain", suffix=".txt")
    assert first == second
    assert store.resolve(first).read_bytes() == b"raw evidence"
    with pytest.raises(ValueError, match="Unsafe artifact suffix"):
        store.put_bytes(b"bad", suffix="../../escape")
