"""Persistence and active-revision projection for external measurement evidence."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from autonomous_lab.domain.enums import MeasurementSourceType
from autonomous_lab.domain.measurements import Measurement
from autonomous_lab.measurement_ingestion.models import (
    ExpectedMeasurementPlan,
    MeasurementAvailability,
    MeasurementBatchKind,
    RawMeasurement,
    RawMeasurementBatch,
)
from autonomous_lab.measurement_ingestion.validation import reconcile_measurements
from autonomous_lab.provenance.hashing import canonical_sha256
from autonomous_lab.storage._serialization import jsonable
from autonomous_lab.storage.orm import (
    AuditEventRecord,
    ExpectedMeasurementPlanRecord,
    ExperimentRecord,
    ExternalMeasurementRevisionRecord,
    MeasurementIngestionBatchRecord,
    ProtocolRecord,
    utc_now,
    uuid_string,
)


class MeasurementIngestionError(RuntimeError):
    """A batch cannot be appended without violating provenance invariants."""


class IncompleteMeasurementPlanError(MeasurementIngestionError):
    """Active evidence does not provide every required available value."""


class MeasurementIngestionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add_plan(
        self,
        plan: ExpectedMeasurementPlan,
        *,
        version: int = 1,
    ) -> ExpectedMeasurementPlanRecord:
        if canonical_sha256(plan, exclude={"plan_sha256"}) != plan.plan_sha256:
            raise MeasurementIngestionError("measurement plan hash is invalid")
        experiment = self.session.get(ExperimentRecord, str(plan.experiment_id))
        if experiment is None:
            raise MeasurementIngestionError(f"experiment {plan.experiment_id} does not exist")
        protocol = self.session.get(ProtocolRecord, str(plan.protocol_id))
        if protocol is None:
            raise MeasurementIngestionError(f"protocol {plan.protocol_id} does not exist")
        if protocol.experiment_id != experiment.id:
            raise MeasurementIngestionError(
                "measurement plan protocol belongs to another experiment"
            )
        if protocol.protocol_hash != plan.protocol_hash:
            raise MeasurementIngestionError("measurement plan protocol hash does not match storage")
        record = ExpectedMeasurementPlanRecord(
            id=str(plan.plan_id),
            experiment_id=str(plan.experiment_id),
            protocol_id=str(plan.protocol_id),
            version=version,
            schema_version=plan.schema_version,
            protocol_hash=plan.protocol_hash,
            plan_sha256=plan.plan_sha256,
            plan_snapshot=jsonable(plan),
            created_at=plan.created_at,
        )
        self.session.add(record)
        self.session.add(
            AuditEventRecord(
                campaign_id=experiment.campaign_id,
                experiment_id=experiment.id,
                entity_type="expected_measurement_plan",
                entity_id=record.id,
                event_type="measurement_plan.created",
                details={"plan_sha256": record.plan_sha256, "version": version},
                occurred_at=plan.created_at,
            )
        )
        self.session.flush()
        return record

    def get_plan(self, plan_id: str | UUID) -> ExpectedMeasurementPlanRecord:
        record = self.session.get(ExpectedMeasurementPlanRecord, str(plan_id))
        if record is None:
            raise MeasurementIngestionError(f"measurement plan {plan_id} does not exist")
        return record

    def latest_plan(self, experiment_id: str | UUID) -> ExpectedMeasurementPlanRecord:
        record = self.session.scalar(
            select(ExpectedMeasurementPlanRecord)
            .where(ExpectedMeasurementPlanRecord.experiment_id == str(experiment_id))
            .order_by(ExpectedMeasurementPlanRecord.version.desc())
            .limit(1)
        )
        if record is None:
            raise MeasurementIngestionError(
                f"experiment {experiment_id} has no expected measurement plan"
            )
        return record

    def plans_for_experiment(
        self, experiment_id: str | UUID
    ) -> tuple[ExpectedMeasurementPlanRecord, ...]:
        return tuple(
            self.session.scalars(
                select(ExpectedMeasurementPlanRecord)
                .where(ExpectedMeasurementPlanRecord.experiment_id == str(experiment_id))
                .order_by(ExpectedMeasurementPlanRecord.version)
            )
        )

    def get_batch(self, batch_id: str | UUID) -> MeasurementIngestionBatchRecord:
        record = self.session.get(MeasurementIngestionBatchRecord, str(batch_id))
        if record is None:
            raise MeasurementIngestionError(f"measurement batch {batch_id} does not exist")
        return record

    def batches_for_experiment(
        self, experiment_id: str | UUID
    ) -> tuple[MeasurementIngestionBatchRecord, ...]:
        return tuple(
            self.session.scalars(
                select(MeasurementIngestionBatchRecord)
                .where(MeasurementIngestionBatchRecord.experiment_id == str(experiment_id))
                .order_by(MeasurementIngestionBatchRecord.submitted_at)
            )
        )

    def add_batch(
        self,
        batch: RawMeasurementBatch,
    ) -> tuple[MeasurementIngestionBatchRecord, tuple[ExternalMeasurementRevisionRecord, ...]]:
        if canonical_sha256(batch, exclude={"batch_sha256"}) != batch.batch_sha256:
            raise MeasurementIngestionError("measurement batch hash is invalid")
        plan_record = self.get_plan(batch.plan_id)
        plan = ExpectedMeasurementPlan.model_validate_json(json.dumps(plan_record.plan_snapshot))
        if str(batch.experiment_id) != plan_record.experiment_id:
            raise MeasurementIngestionError("batch experiment does not match its plan")
        issues = reconcile_measurements(plan, batch.measurements, batch_kind=batch.batch_kind)
        if issues:
            codes = ", ".join(sorted({issue.code for issue in issues}))
            raise MeasurementIngestionError(f"batch does not reconcile with plan: {codes}")
        if batch.source_file_sha256 is not None:
            duplicate = self.session.scalar(
                select(MeasurementIngestionBatchRecord.id).where(
                    MeasurementIngestionBatchRecord.experiment_id == str(batch.experiment_id),
                    MeasurementIngestionBatchRecord.source_file_sha256 == batch.source_file_sha256,
                )
            )
            if duplicate is not None:
                raise MeasurementIngestionError(
                    f"source file was already imported in batch {duplicate}"
                )

        batch_record = MeasurementIngestionBatchRecord(
            id=str(batch.batch_id),
            experiment_id=str(batch.experiment_id),
            plan_id=str(batch.plan_id),
            source_type=batch.source_type.value,
            batch_kind=batch.batch_kind.value,
            submitted_by=batch.submitted_by,
            submitted_at=batch.submitted_at,
            source_file_sha256=batch.source_file_sha256,
            source_filename=batch.source_filename,
            batch_sha256=batch.batch_sha256,
            batch_snapshot=jsonable(batch),
            created_at=utc_now(),
        )
        self.session.add(batch_record)
        self.session.flush()

        revisions: list[ExternalMeasurementRevisionRecord] = []
        for row in batch.measurements:
            active = self._active_revision(batch.plan_id, row.expected_measurement_id)
            if batch.batch_kind is MeasurementBatchKind.INITIAL and active is not None:
                raise MeasurementIngestionError(
                    f"initial value {row.expected_measurement_id!r} already exists"
                )
            if batch.batch_kind is MeasurementBatchKind.CORRECTION and active is None:
                raise MeasurementIngestionError(
                    f"cannot correct missing value {row.expected_measurement_id!r}"
                )
            revision_number = 1 if active is None else active.revision_number + 1
            revision_id = uuid_string()
            revision_values = {
                "revision_id": revision_id,
                "batch_sha256": batch.batch_sha256,
                "expected_measurement_id": row.expected_measurement_id,
                "revision_number": revision_number,
                "supersedes_revision_id": None if active is None else active.id,
                "raw_measurement": row.model_dump(mode="python"),
                "source_type": batch.source_type,
            }
            revision = ExternalMeasurementRevisionRecord(
                id=revision_id,
                experiment_id=str(batch.experiment_id),
                plan_id=str(batch.plan_id),
                batch_id=batch_record.id,
                expected_measurement_id=row.expected_measurement_id,
                revision_number=revision_number,
                supersedes_revision_id=None if active is None else active.id,
                source_type=batch.source_type.value,
                availability=row.availability.value,
                value=None if row.value is None else float(row.value),
                value_decimal=None if row.value is None else str(row.value),
                unit=row.unit,
                measured_at=row.measured_at,
                correction_reason=row.correction_reason,
                revision_sha256=canonical_sha256(revision_values),
                raw_snapshot=jsonable(row),
                created_at=batch.submitted_at,
            )
            self.session.add(revision)
            self.session.flush()
            revisions.append(revision)

        experiment = cast(
            ExperimentRecord,
            self.session.get(ExperimentRecord, batch_record.experiment_id),
        )
        self.session.add(
            AuditEventRecord(
                campaign_id=experiment.campaign_id,
                experiment_id=experiment.id,
                entity_type="measurement_ingestion_batch",
                entity_id=batch_record.id,
                event_type="measurements.submitted",
                actor=batch.submitted_by,
                details={
                    "source_type": batch.source_type.value,
                    "batch_kind": batch.batch_kind.value,
                    "batch_sha256": batch.batch_sha256,
                    "source_file_sha256": batch.source_file_sha256,
                    "revision_ids": [item.id for item in revisions],
                },
                occurred_at=batch.submitted_at,
            )
        )
        return batch_record, tuple(revisions)

    def active_revisions(
        self, experiment_id: str | UUID
    ) -> tuple[ExternalMeasurementRevisionRecord, ...]:
        plan = self.latest_plan(experiment_id)
        successor = aliased(ExternalMeasurementRevisionRecord)
        rows = self.session.scalars(
            select(ExternalMeasurementRevisionRecord)
            .outerjoin(
                successor,
                successor.supersedes_revision_id == ExternalMeasurementRevisionRecord.id,
            )
            .where(
                ExternalMeasurementRevisionRecord.plan_id == plan.id,
                successor.id.is_(None),
            )
            .order_by(ExternalMeasurementRevisionRecord.expected_measurement_id)
        )
        return tuple(rows)

    def revision_history(
        self,
        experiment_id: str | UUID,
        expected_measurement_id: str,
    ) -> tuple[ExternalMeasurementRevisionRecord, ...]:
        plan = self.latest_plan(experiment_id)
        return tuple(
            self.session.scalars(
                select(ExternalMeasurementRevisionRecord)
                .where(
                    ExternalMeasurementRevisionRecord.plan_id == plan.id,
                    ExternalMeasurementRevisionRecord.expected_measurement_id
                    == expected_measurement_id,
                )
                .order_by(ExternalMeasurementRevisionRecord.revision_number)
            )
        )

    def active_measurements(
        self,
        experiment_id: str | UUID,
        run_id: str | UUID,
    ) -> tuple[Measurement, ...]:
        """Project a complete active revision set into the existing QC domain type."""

        plan_record = self.latest_plan(experiment_id)
        plan = ExpectedMeasurementPlan.model_validate_json(json.dumps(plan_record.plan_snapshot))
        active = {
            item.expected_measurement_id: item for item in self.active_revisions(experiment_id)
        }
        incomplete = [
            expected.expected_measurement_id
            for expected in plan.entries
            if expected.required
            and (
                expected.expected_measurement_id not in active
                or active[expected.expected_measurement_id].availability
                != MeasurementAvailability.AVAILABLE.value
            )
        ]
        if incomplete:
            raise IncompleteMeasurementPlanError(
                "required measurements are missing or unavailable: " + ", ".join(incomplete)
            )
        projected: list[Measurement] = []
        for expected in plan.entries:
            revision = active.get(expected.expected_measurement_id)
            if revision is None or revision.availability != MeasurementAvailability.AVAILABLE.value:
                continue
            raw = RawMeasurement.model_validate_json(json.dumps(revision.raw_snapshot))
            if revision.value_decimal is not None:
                raw = raw.model_copy(update={"value": Decimal(revision.value_decimal)})
            assert raw.value is not None
            batch = self.session.get(MeasurementIngestionBatchRecord, revision.batch_id)
            if batch is None:
                raise MeasurementIngestionError(f"batch {revision.batch_id} does not exist")
            source = MeasurementSourceType(revision.source_type)
            projected.append(
                Measurement(
                    measurement_id=UUID(revision.id),
                    experiment_id=UUID(str(experiment_id)),
                    run_id=UUID(str(run_id)),
                    step_id=expected.step_id,
                    device_id=_source_identifier(source, batch, raw),
                    vessel_id=expected.vessel_id,
                    role=expected.sample_role,
                    analyte_level=expected.analyte_level,
                    replicate_index=expected.replicate_index,
                    measurement_name=expected.measurement_type,
                    raw_value=raw.value,
                    unit=raw.unit,
                    measured_at=raw.measured_at,
                    metadata={
                        "measurement_source": source.value,
                        "expected_measurement_id": expected.expected_measurement_id,
                        "measurement_plan_id": plan.plan_id,
                        "measurement_plan_sha256": plan.plan_sha256,
                        "ingestion_batch_id": batch.id,
                        "ingestion_batch_sha256": batch.batch_sha256,
                        "source_file_sha256": batch.source_file_sha256,
                        "revision_id": revision.id,
                        "revision_number": revision.revision_number,
                        "revision_sha256": revision.revision_sha256,
                        "supersedes_revision_id": revision.supersedes_revision_id,
                        "wavelength": (
                            None
                            if expected.wavelength is None
                            else expected.wavelength.model_dump()
                        ),
                        "note": raw.note,
                        "source_metadata": raw.source_metadata,
                    },
                )
            )
        # The plan entry order is scientifically meaningful and stable.
        return tuple(projected)

    def _active_revision(
        self,
        plan_id: str | UUID,
        expected_measurement_id: str,
    ) -> ExternalMeasurementRevisionRecord | None:
        successor = aliased(ExternalMeasurementRevisionRecord)
        return self.session.scalar(
            select(ExternalMeasurementRevisionRecord)
            .outerjoin(
                successor,
                successor.supersedes_revision_id == ExternalMeasurementRevisionRecord.id,
            )
            .where(
                ExternalMeasurementRevisionRecord.plan_id == str(plan_id),
                ExternalMeasurementRevisionRecord.expected_measurement_id
                == expected_measurement_id,
                successor.id.is_(None),
            )
        )


def _source_identifier(
    source: MeasurementSourceType,
    batch: MeasurementIngestionBatchRecord,
    raw: RawMeasurement,
) -> str:
    if source is MeasurementSourceType.MANUAL_ENTRY:
        return f"manual-entry:{batch.submitted_by}"
    if source is MeasurementSourceType.FILE_IMPORT:
        return f"file-import:{batch.source_filename}"
    supplied = raw.source_metadata.get("device_id") or raw.source_metadata.get("source_id")
    return f"{source.value}:{supplied or 'unspecified'}"
