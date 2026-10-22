from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from sqlalchemy import func, select

from autonomous_lab.campaigns.loader import load_campaign, load_validation_policy
from autonomous_lab.campaigns.service import CampaignService
from autonomous_lab.domain.enums import CampaignState, ExperimentState
from autonomous_lab.human import (
    AcknowledgementDecision,
    HumanExecutionBackend,
    VirtualOperator,
)
from autonomous_lab.human.service import HumanWorkflowError, HumanWorkflowService
from autonomous_lab.measurement_ingestion.csv_import import parse_measurement_csv
from autonomous_lab.measurement_ingestion.manual import ManualBatchBuilder
from autonomous_lab.measurement_ingestion.models import (
    MeasurementBatchKind,
    RawMeasurementBatch,
)
from autonomous_lab.measurement_ingestion.repository import MeasurementIngestionError
from autonomous_lab.measurement_ingestion.service import MeasurementWorkflowService
from autonomous_lab.measurement_ingestion.sources import (
    acquisition_context,
    mock_physical_sensor_source_from_rows,
)
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
            ),
            "optimization": loaded.config.optimization.model_copy(
                update={"startup_experiments": min(budget, 4)}
            ),
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
    workflow.submit(_manual_fixture_batch(workflow, experiment_id))
    storage.close()


def _manual_fixture_batch(
    workflow: MeasurementWorkflowService,
    experiment_id: UUID,
) -> RawMeasurementBatch:
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
    return result.batch


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
    source = mock_physical_sensor_source_from_rows(
        "mock-optical-e2e",
        fixture_result.batch.measurements,
        fixture_name=FIXTURE_PATH.name,
        fixture_sha256=fixture_result.source_file_sha256 or "",
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


def test_manual_campaign_survives_process_boundaries_and_observes_once(
    tmp_path: Path,
) -> None:
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
        artifact_count = uow.session.scalar(select(func.count(ArtifactRecord.id)))
        assert artifact_count is not None and artifact_count >= 16
        hashes = list(
            uow.session.scalars(select(MeasurementIngestionBatchRecord.batch_sha256))
        )
        assert len(hashes) == 3
        assert all(len(item) == 64 for item in hashes)
    storage.close()


def test_mock_sensor_campaign_runs_three_external_instrument_iterations(
    tmp_path: Path,
) -> None:
    campaign_id = _exercise_three_boundaries(
        tmp_path,
        config="assay-reagent-reduction-mock-sensor.yaml",
        sensor=True,
    )
    storage, service = _service(tmp_path)
    status = service.status(campaign_id)
    assert status.valid_observation_count == 3
    report = service.report(campaign_id)
    origins = report["evidence_origins"]
    assert isinstance(origins, dict)
    assert origins["measurements"] == {"mock_physical_sensor": 81}
    with UnitOfWork(storage.database) as uow:
        assert uow.session is not None
        sources = list(
            uow.session.scalars(select(MeasurementIngestionBatchRecord.source_type))
        )
        assert sources == ["physical_sensor", "physical_sensor", "physical_sensor"]
        batches = list(
            uow.session.scalars(select(MeasurementIngestionBatchRecord.batch_snapshot))
        )
        expected_fixture_hash = hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest()
        assert all(
            batch["measurements"][0]["source_metadata"]["fixture_sha256"]
            == expected_fixture_hash
            for batch in batches
        )
        assert all(
            batch["measurements"][0]["source_metadata"]["calibration_record_sha256"]
            == batch["measurements"][0]["source_metadata"]["sensor_evidence"][
                "calibration_sha256"
            ]
            for batch in batches
        )
        assert all(
            batch["measurements"][0]["source_metadata"]["calibration_record"][
                "sensor_id"
            ]
            == "mock-optical-e2e"
            for batch in batches
        )
    storage.close()


def test_group_acknowledgements_resume_after_process_restart(tmp_path: Path) -> None:
    storage, service = _service(tmp_path)
    campaign_id = service.create(_loaded("assay-reagent-reduction-manual.yaml", budget=1))
    assert asyncio.run(service.run(campaign_id)).state is CampaignState.PAUSED
    experiment = _pending(storage, campaign_id)
    workflow = HumanWorkflowService(
        storage,
        worklist_root=tmp_path / "worklists",
        artifact_root=tmp_path / "artifacts",
    )
    experiment_id = UUID(experiment.id)
    workflow.start_execution(experiment_id, operator_id="restart-operator")
    backend = HumanExecutionBackend()
    session = workflow.execution_session(experiment_id, operator_id="restart-operator")
    for group in session.worklist.procedure[:2]:
        session = backend.acknowledge(
            session,
            group_id=group.group_id,
            decision=AcknowledgementDecision.CONFIRM,
        )
        workflow.persist_execution_session(session)
    storage.close()

    storage, _ = _service(tmp_path)
    workflow = HumanWorkflowService(
        storage,
        worklist_root=tmp_path / "worklists",
        artifact_root=tmp_path / "artifacts",
    )
    restored = workflow.execution_session(
        experiment_id,
        operator_id="restart-operator",
    )
    assert [item.group_id for item in restored.acknowledgements] == [
        item.group_id for item in restored.worklist.procedure[:2]
    ]
    for group in restored.worklist.procedure[2:]:
        restored = backend.acknowledge(
            restored,
            group_id=group.group_id,
            decision=AcknowledgementDecision.CONFIRM,
        )
        workflow.persist_execution_session(restored)
    workflow.complete_execution(backend.complete(restored))
    with storage.unit_of_work() as uow:
        assert uow.experiments.get(experiment.id).state == "awaiting_measurements"
        uow.commit()
    storage.close()


def test_human_completion_rejects_forged_step_evidence(tmp_path: Path) -> None:
    storage, service = _service(tmp_path)
    campaign_id = service.create(_loaded("assay-reagent-reduction-manual.yaml", budget=1))
    assert asyncio.run(service.run(campaign_id)).state is CampaignState.PAUSED
    experiment = _pending(storage, campaign_id)
    workflow = HumanWorkflowService(
        storage,
        worklist_root=tmp_path / "worklists",
        artifact_root=tmp_path / "artifacts",
    )
    experiment_id = UUID(experiment.id)
    worklist = workflow.load_worklist(experiment_id)
    workflow.start_execution(experiment_id, operator_id="truth-operator")
    record = VirtualOperator("truth-operator").confirm(worklist)
    forged_step = record.step_evidence[0].model_copy(
        update={"observed_action": "fabricated completion"}
    )
    forged = record.model_copy(
        update={"step_evidence": (forged_step, *record.step_evidence[1:])}
    )

    with pytest.raises(HumanWorkflowError, match="canonical evidence"):
        workflow.complete_execution(forged)
    with storage.unit_of_work() as uow:
        assert uow.experiments.get(experiment.id).state == "running"
        uow.commit()
    storage.close()


def test_unavailable_measurement_waits_for_immutable_correction(tmp_path: Path) -> None:
    storage, service = _service(tmp_path)
    campaign_id = service.create(_loaded("assay-reagent-reduction-manual.yaml", budget=1))
    assert asyncio.run(service.run(campaign_id)).state is CampaignState.PAUSED
    experiment = _pending(storage, campaign_id)
    experiment_id = UUID(experiment.id)
    _confirm_operator(tmp_path, experiment_id)

    workflow = MeasurementWorkflowService(storage)
    complete = _manual_fixture_batch(workflow, experiment_id)
    builder = ManualBatchBuilder(
        workflow.plan(experiment_id),
        "fixture-operator",
        submitted_at=datetime.now(UTC),
    )
    missing_id = complete.measurements[-1].expected_measurement_id
    for row in complete.measurements[:-1]:
        assert row.value is not None
        builder.set_value(
            row.expected_measurement_id,
            row.value,
            measured_at=row.measured_at,
            unit=row.unit,
        )
    builder.set_unavailable(
        missing_id,
        "instrument export omitted this reading",
        measured_at=datetime.now(UTC),
    )
    unavailable = builder.finalize().batch
    assert unavailable is not None
    workflow.submit(unavailable)
    with storage.unit_of_work() as uow:
        assert uow.experiments.get(experiment.id).state == "awaiting_measurements"
        uow.commit()

    original = complete.measurements[-1]
    assert original.value is not None
    correction = ManualBatchBuilder(
        workflow.plan(experiment_id),
        "fixture-operator",
        batch_kind=MeasurementBatchKind.CORRECTION,
        submitted_at=datetime.now(UTC),
    )
    correction.set_value(
        missing_id,
        original.value,
        measured_at=datetime.now(UTC),
        unit=original.unit,
        correction_reason="recovered reading from immutable instrument export",
    )
    corrected = correction.finalize().batch
    assert corrected is not None
    workflow.submit(corrected)
    with storage.unit_of_work() as uow:
        history = uow.measurement_ingestion.revision_history(experiment.id, missing_id)
        assert [item.availability for item in history] == ["unavailable", "available"]
        assert uow.experiments.get(experiment.id).state == "measured"
        uow.commit()
    storage.close()


def test_measurements_cannot_precede_execution_and_abort_is_never_observed(
    tmp_path: Path,
) -> None:
    storage, service = _service(tmp_path)
    campaign_id = service.create(_loaded("assay-reagent-reduction-manual.yaml", budget=2))
    status = asyncio.run(service.run(campaign_id))
    assert status.state is CampaignState.PAUSED
    experiment = _pending(storage, campaign_id)
    workflow = MeasurementWorkflowService(storage)
    batch = _manual_fixture_batch(workflow, UUID(experiment.id))

    with pytest.raises(MeasurementIngestionError, match="only after completed"):
        workflow.submit(batch)
    with storage.unit_of_work() as uow:
        assert uow.experiments.get(experiment.id).state == "awaiting_execution"
        uow.commit()
    human = HumanWorkflowService(
        storage,
        worklist_root=tmp_path / "worklists",
        artifact_root=tmp_path / "artifacts",
    )
    human.abort_execution(
        UUID(experiment.id),
        operator_id="fixture-operator",
        reason="deliberate abort fixture",
    )
    storage.close()

    storage, service = _service(tmp_path)
    resumed = asyncio.run(service.run(campaign_id))
    assert resumed.state is CampaignState.PAUSED
    assert resumed.experiment_count == 2
    assert resumed.valid_observation_count == 0
    assert resumed.aborted_count == 1
    with storage.unit_of_work() as uow:
        assert uow.experiments.get(experiment.id).state == "aborted"
        uow.commit()
    assert _pending(storage, campaign_id).ordinal == 1
    storage.close()
