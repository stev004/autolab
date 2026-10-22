from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text

from autonomous_lab.domain.enums import MeasurementRole
from autonomous_lab.domain.quantities import QuantityValue
from autonomous_lab.measurement_ingestion import (
    ExpectedMeasurement,
    ManualBatchBuilder,
    MeasurementBatchKind,
    make_expected_plan,
    parse_measurement_csv,
)
from autonomous_lab.measurement_ingestion.repository import (
    IncompleteMeasurementPlanError,
    MeasurementIngestionError,
)
from autonomous_lab.storage import Database, ImmutableRecordError, UnitOfWork

CAMPAIGN_ID = "00000000-0000-0000-0000-000000000201"
EXPERIMENT_ID = "00000000-0000-0000-0000-000000000202"
PROTOCOL_ID = "00000000-0000-0000-0000-000000000203"
RUN_ID = "00000000-0000-0000-0000-000000000204"
NOW = datetime(2026, 8, 14, 18, 42, tzinfo=UTC)


@pytest.fixture
def database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "ingestion.sqlite3")
    database.create_schema()
    with UnitOfWork(database) as uow:
        uow.campaigns.add(
            campaign_id=CAMPAIGN_ID,
            name="manual assay",
            state="ready",
            schema_version="1",
            config_source="execution: human_manual",
            config={"execution": "human_manual"},
            objective={"minimize": ["cost"]},
            parameter_space={"volume": {"minimum": 20, "maximum": 200}},
        )
        uow.experiments.add(
            experiment_id=EXPERIMENT_ID,
            campaign_id=CAMPAIGN_ID,
            ordinal=0,
            state="awaiting_measurements",
        )
        uow.records.add_protocol(
            protocol_id=PROTOCOL_ID,
            experiment_id=EXPERIMENT_ID,
            schema_version="1",
            protocol={"steps": [{"command_type": "measure_absorbance"}]},
            compiler_name="assay",
            compiler_version="1",
        )
        uow.commit()
    try:
        yield database
    finally:
        database.dispose()


def _add_plan(database: Database):
    with UnitOfWork(database) as uow:
        protocol = uow.records.experiment_evidence(EXPERIMENT_ID)["protocols"][0]
        plan = make_expected_plan(
            experiment_id=UUID(EXPERIMENT_ID),
            protocol_id=UUID(PROTOCOL_ID),
            protocol_hash=protocol.protocol_hash,
            entries=tuple(
                ExpectedMeasurement(
                    expected_measurement_id=f"measure:{sample}",
                    sample_id=sample,
                    vessel_id=sample,
                    step_id="measure",
                    sample_role=MeasurementRole.BLANK,
                    measurement_type="absorbance",
                    wavelength=QuantityValue(value=520, unit="nanometer"),
                    unit="AU",
                    replicate_index=index,
                )
                for index, sample in enumerate(("B01", "B02"))
            ),
            created_at=NOW,
        )
        uow.measurement_ingestion.add_plan(plan)
        uow.commit()
    return plan


def _complete_initial(plan):
    builder = ManualBatchBuilder(plan, "operator-1", submitted_at=NOW)
    builder.set_value("measure:B01", Decimal("0.031"), measured_at=NOW)
    builder.set_value("measure:B02", Decimal("0.029"), measured_at=NOW)
    result = builder.finalize()
    assert result.batch is not None
    return result.batch


def test_revision_chain_preserves_original_and_projects_only_active_values(
    database: Database,
) -> None:
    plan = _add_plan(database)
    initial = _complete_initial(plan)
    with UnitOfWork(database) as uow:
        _, revisions = uow.measurement_ingestion.add_batch(initial)
        original_id = revisions[0].id
        uow.commit()

    correction = ManualBatchBuilder(
        plan,
        "operator-1",
        batch_kind=MeasurementBatchKind.CORRECTION,
        submitted_at=datetime(2026, 8, 14, 19, 0, tzinfo=UTC),
    )
    correction.set_value(
        "measure:B01",
        Decimal("0.0031"),
        measured_at=NOW,
        correction_reason="decimal-point transcription error",
    )
    correction_result = correction.finalize()
    assert correction_result.batch is not None
    with UnitOfWork(database) as uow:
        _, revisions = uow.measurement_ingestion.add_batch(correction_result.batch)
        assert revisions[0].supersedes_revision_id == original_id
        uow.commit()

    with UnitOfWork(database) as uow:
        history = uow.measurement_ingestion.revision_history(EXPERIMENT_ID, "measure:B01")
        active = uow.measurement_ingestion.active_revisions(EXPERIMENT_ID)
        projected = uow.measurement_ingestion.active_measurements(EXPERIMENT_ID, RUN_ID)
        assert [item.revision_number for item in history] == [1, 2]
        assert len(active) == 2
        assert projected[0].raw_value == Decimal("0.0031")
        assert projected[0].device_id == "manual-entry:operator-1"
        assert projected[0].metadata["supersedes_revision_id"] == original_id
        assert projected[0].metadata["measurement_source"] == "manual_entry"


def test_raw_revisions_are_immutable(database: Database) -> None:
    plan = _add_plan(database)
    with UnitOfWork(database) as uow:
        uow.measurement_ingestion.add_batch(_complete_initial(plan))
        uow.commit()
    with pytest.raises(ImmutableRecordError), UnitOfWork(database) as uow:
        revision = uow.measurement_ingestion.revision_history(EXPERIMENT_ID, "measure:B01")[0]
        revision.value = 999.0
        uow.commit()


def test_incomplete_or_unavailable_plan_cannot_be_projected(database: Database) -> None:
    plan = _add_plan(database)
    builder = ManualBatchBuilder(plan, "operator-1", submitted_at=NOW)
    builder.set_value("measure:B01", Decimal("0.031"), measured_at=NOW)
    builder.set_unavailable("measure:B02", "sample spilled", measured_at=NOW)
    result = builder.finalize()
    assert result.batch is not None
    with UnitOfWork(database) as uow:
        uow.measurement_ingestion.add_batch(result.batch)
        uow.commit()
    with (
        UnitOfWork(database) as uow,
        pytest.raises(IncompleteMeasurementPlanError, match="measure:B02"),
    ):
        uow.measurement_ingestion.active_measurements(EXPERIMENT_ID, RUN_ID)


def test_initial_values_cannot_be_overwritten_without_correction(database: Database) -> None:
    plan = _add_plan(database)
    batch = _complete_initial(plan)
    with UnitOfWork(database) as uow:
        uow.measurement_ingestion.add_batch(batch)
        uow.commit()
    replacement = _complete_initial(plan).model_copy(
        update={"batch_id": UUID("00000000-0000-0000-0000-000000000299")}
    )
    # model_copy intentionally bypasses validation; repository independently
    # verifies the hash and refuses the tampered envelope.
    with (
        UnitOfWork(database) as uow,
        pytest.raises(MeasurementIngestionError, match="batch hash"),
    ):
        uow.measurement_ingestion.add_batch(replacement)


def test_imported_file_hash_and_source_survive_projection(database: Database) -> None:
    plan = _add_plan(database)
    content = (
        b"sample_id,measurement_type,wavelength_nm,value,unit,timestamp\n"
        b"B01,absorbance,520,0.031,AU,2026-08-14T18:42:00Z\n"
        b"B02,absorbance,520,0.029,AU,2026-08-14T18:42:20Z\n"
    )
    result = parse_measurement_csv(
        content,
        plan=plan,
        submitted_by="plate-reader-export",
        source_filename="plate-01.csv",
        submitted_at=NOW,
    )
    assert result.batch is not None
    with UnitOfWork(database) as uow:
        uow.measurement_ingestion.add_batch(result.batch)
        uow.commit()
    with UnitOfWork(database) as uow:
        batches = uow.measurement_ingestion.batches_for_experiment(EXPERIMENT_ID)
        projected = uow.measurement_ingestion.active_measurements(EXPERIMENT_ID, RUN_ID)
        evidence = uow.records.experiment_evidence(EXPERIMENT_ID)
        assert batches[0].source_file_sha256 == result.source_file_sha256
        assert projected[0].device_id == "file-import:plate-01.csv"
        assert projected[0].metadata["source_file_sha256"] == result.source_file_sha256
        assert len(evidence["external_measurement_revisions"]) == 2
    with (
        UnitOfWork(database) as uow,
        pytest.raises(MeasurementIngestionError, match="already imported"),
    ):
        uow.measurement_ingestion.add_batch(result.batch)
        uow.commit()


def test_high_precision_manual_decimal_survives_float_index_projection(
    database: Database,
) -> None:
    plan = _add_plan(database)
    precise = Decimal("0.0310000000000000000000000001")
    builder = ManualBatchBuilder(plan, "operator-1", submitted_at=NOW)
    builder.set_value("measure:B01", precise, measured_at=NOW)
    builder.set_value("measure:B02", Decimal("0.029"), measured_at=NOW)
    batch = builder.finalize().batch
    assert batch is not None
    with UnitOfWork(database) as uow:
        uow.measurement_ingestion.add_batch(batch)
        uow.commit()
    with UnitOfWork(database) as uow:
        revisions = uow.measurement_ingestion.revision_history(EXPERIMENT_ID, "measure:B01")
        projected = uow.measurement_ingestion.active_measurements(EXPERIMENT_ID, RUN_ID)
        assert revisions[0].value_decimal == str(precise)
        assert projected[0].raw_value == precise


def test_projection_rejects_direct_sql_tampering_with_exact_value(
    database: Database,
) -> None:
    plan = _add_plan(database)
    with UnitOfWork(database) as uow:
        uow.measurement_ingestion.add_batch(_complete_initial(plan))
        uow.commit()
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE external_measurement_revisions "
                "SET value_decimal = '9.99' WHERE expected_measurement_id = 'measure:B01'"
            )
        )

    with UnitOfWork(database) as uow, pytest.raises(
        MeasurementIngestionError, match="SHA-256"
    ):
        uow.measurement_ingestion.active_measurements(EXPERIMENT_ID, RUN_ID)
