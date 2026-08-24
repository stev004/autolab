from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, select

from autonomous_lab.campaigns.loader import load_campaign, load_validation_policy
from autonomous_lab.campaigns.service import CampaignService
from autonomous_lab.cli import _mock_sensor_source
from autonomous_lab.domain.enums import CampaignState, ExperimentState
from autonomous_lab.human import VirtualOperator
from autonomous_lab.human.service import HumanWorkflowService
from autonomous_lab.measurement_ingestion.csv_import import parse_measurement_csv
from autonomous_lab.measurement_ingestion.manual import ManualBatchBuilder
from autonomous_lab.measurement_ingestion.service import MeasurementWorkflowService
from autonomous_lab.measurement_ingestion.sources import acquisition_context
from autonomous_lab.storage import Database, Storage, UnitOfWork
from autonomous_lab.storage.orm import (
    ArtifactRecord,
    ExperimentRecord,
    MeasurementIngestionBatchRecord,
    OptimizerObservationRecord,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = PROJECT_ROOT / "configs" / "validation-policy.yaml"
FIXTURE_PATH = PROJECT_ROOT / "experiments" / "fixtures" / "mock-optical-readings.csv"


def _loaded(path: str, *, budget: int = 4):
    loaded = load_campaign(PROJECT_ROOT / "configs" / path)
    config = loaded.config.model_copy(
        update={
            "campaign": loaded.config.campaign.model_copy(
                update={"max_experiments": budget}
            )
        }
    )
    return replace(loaded, config=config)


def _service(root: Path) -> tuple[Storage, CampaignService]:
    storage = Storage(Database(root / "autolab.sqlite3"))
    storage.initialize()
    return storage, CampaignService(
        storage,
        validation_policy=load_validation_policy(POLICY_PATH),
        optimizer_root=root / "optimizer",
        prefer_gp=False,
        worklist_root=root / "worklists",
        artifact_root=root / "artifacts",
    )


def _pending(storage: Storage, campaign_id: UUID) -> ExperimentRecord:
    with UnitOfWork(storage.database) as uow:
        assert uow.session is not None
        row = uow.session.scalar(
            select(ExperimentRecord)
            .where(
                ExperimentRecord.campaign_id == str(campaign_id),
                ExperimentRecord.state == ExperimentState.AWAITING_EXECUTION.value,
            )
            .order_by(ExperimentRecord.ordinal.desc())
        )
        assert row is not None
        uow.session.expunge(row)
        uow.commit()
    return row


def _confirm_operator(root: Path, experiment_id: UUID) -> None:
    storage = Storage(Database(root / "autolab.sqlite3"))
    workflow = HumanWorkflowService(
        storage,
        worklist_root=root / "worklists",
        artifact_root=root / "artifacts",
    )
    worklist = workflow.load_worklist(experiment_id)
    workflow.start_execution(experiment_id, operator_id="fixture-operator")
    workflow.complete_execution(VirtualOperator("fixture-operator").confirm(worklist))
    storage.close()


def _submit_manual_fixture(root: Path, experiment_id: UUID) -> None:
    storage = Storage(Database(root / "autolab.sqlite3"))
    workflow = MeasurementWorkflowService(storage)
    plan = workflow.plan(experiment_id)
    parsed = parse_measurement_csv(
        FIXTURE_PATH.read_bytes(),
        plan=plan,
        submitted_by="fixture-data-reader",
        source_filename=FIXTURE_PATH.name,
        submitted_at=datetime.now(UTC),
    )
    assert parsed.batch is not None
    builder = ManualBatchBuilder(plan, "fixture-operator", submitted_at=datetime.now(UTC))
    for row in parsed.batch.measurements:
        assert row.value is not None
        builder.set_value(
            row.expected_measurement_id,
            row.value,
            measured_at=row.measured_at,
            unit=row.unit,
            source_metadata={"entered_by_virtual_operator_fixture": True},
        )
    result = builder.finalize()
    assert result.batch is not None
    workflow.submit(result.batch)
    storage.close()


def _submit_sensor(root: Path, experiment_id: UUID) -> None:
    storage = Storage(Database(root / "autolab.sqlite3"))
    workflow = MeasurementWorkflowService(storage)
    plan = workflow.plan(experiment_id)
    fixture_result = parse_measurement_csv(
        FIXTURE_PATH.read_bytes(),
        plan=plan,
        submitted_by="fixture-operator",
        source_filename=FIXTURE_PATH.name,
        submitted_at=datetime.now(UTC),
    )
    assert fixture_result.batch is not None
    source = _mock_sensor_source(
        "mock-optical-e2e", fixture_result.batch.measurements
    )
    batch = asyncio.run(
        source.acquire(
            plan,
            acquisition_context(
                experiment_id=experiment_id,
                run_id=uuid5(
                    NAMESPACE_URL, f"autolab:{experiment_id}:human-execution:1"
                ),
                submitted_by="fixture-operator",
                acquired_at=datetime.now(UTC),
            ),
        )
    )
    workflow.submit(batch)
    storage.close()


def _exercise_three_boundaries(root: Path, *, config: str, sensor: bool) -> UUID:
    storage, service = _service(root)
    campaign_id = service.create(_loaded(config))
    storage.close()

    for _ in range(3):
        storage, service = _service(root)
        status = asyncio.run(service.run(campaign_id))
        assert status.state is CampaignState.PAUSED
        experiment = _pending(storage, campaign_id)
        storage.close()

        _confirm_operator(root, UUID(experiment.id))
        if sensor:
            _submit_sensor(root, UUID(experiment.id))
        else:
            _submit_manual_fixture(root, UUID(experiment.id))

        storage, service = _service(root)
        status = asyncio.run(service.run(campaign_id))
        assert status.valid_observation_count == experiment.ordinal + 1
        storage.close()
    return campaign_id


def test_manual_campaign_survives_process_boundaries_and_observes_once(tmp_path) -> None:
    campaign_id = _exercise_three_boundaries(
        tmp_path,
        config="assay-reagent-reduction-manual.yaml",
        sensor=False,
    )
    storage, service = _service(tmp_path)
    status = service.status(campaign_id)
    assert status.state is CampaignState.PAUSED
    assert status.experiment_count == 4
    assert status.valid_observation_count == 3
    with UnitOfWork(storage.database) as uow:
        assert uow.session is not None
        assert uow.session.scalar(select(func.count(OptimizerObservationRecord.id))) == 3
        assert uow.session.scalar(select(func.count(ArtifactRecord.id))) >= 16
        hashes = list(
            uow.session.scalars(select(MeasurementIngestionBatchRecord.batch_sha256))
        )
        assert len(hashes) == 3
        assert all(len(item) == 64 for item in hashes)
    storage.close()


def test_mock_sensor_campaign_runs_three_external_instrument_iterations(tmp_path) -> None:
    campaign_id = _exercise_three_boundaries(
        tmp_path,
        config="assay-reagent-reduction-mock-sensor.yaml",
        sensor=True,
    )
    storage, service = _service(tmp_path)
    status = service.status(campaign_id)
    assert status.valid_observation_count == 3
    with UnitOfWork(storage.database) as uow:
        assert uow.session is not None
        sources = list(
            uow.session.scalars(select(MeasurementIngestionBatchRecord.source_type))
        )
        assert sources == ["physical_sensor", "physical_sensor", "physical_sensor"]
    storage.close()
