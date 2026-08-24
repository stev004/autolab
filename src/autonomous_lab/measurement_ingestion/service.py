"""State-aware application service for immutable external measurement batches."""

from __future__ import annotations

from uuid import UUID

from autonomous_lab.domain.enums import ExperimentState, MeasurementSourceType
from autonomous_lab.domain.state_machines import transition_experiment
from autonomous_lab.measurement_ingestion.models import (
    ExpectedMeasurementPlan,
    MeasurementBatchKind,
    RawMeasurementBatch,
)
from autonomous_lab.measurement_ingestion.repository import MeasurementIngestionError
from autonomous_lab.storage import Storage


class MeasurementWorkflowService:
    """Append a reconciled batch only after truthful human execution evidence."""

    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    def plan(self, experiment_id: UUID) -> ExpectedMeasurementPlan:
        with self.storage.unit_of_work() as uow:
            record = uow.measurement_ingestion.latest_plan(str(experiment_id))
            plan = ExpectedMeasurementPlan.model_validate(record.plan_snapshot, strict=False)
            uow.commit()
        return plan

    def submit(self, batch: RawMeasurementBatch) -> None:
        """Persist one initial/correction batch and mark a fully reconciled plan measured."""

        with self.storage.unit_of_work() as uow:
            experiment = uow.experiments.get(str(batch.experiment_id))
            campaign = uow.campaigns.get(experiment.campaign_id)
            execution = campaign.config_json.get("execution", {})
            mode = execution.get("mode", "simulation")
            configured_source = execution.get("measurement_source")
            allowed_sources = {
                ("human_manual", "manual_entry"): {
                    MeasurementSourceType.MANUAL_ENTRY
                },
                ("human_manual", "csv_import"): {MeasurementSourceType.FILE_IMPORT},
                ("human_sensor", "mock_physical_sensor"): {
                    MeasurementSourceType.PHYSICAL_SENSOR
                },
            }.get((str(mode), str(configured_source)), set())
            if batch.source_type not in allowed_sources:
                raise MeasurementIngestionError(
                    f"measurement source {batch.source_type.value!r} is forbidden for "
                    f"execution mode {mode!r} configured for {configured_source!r}"
                )
            allowed = {ExperimentState.AWAITING_MEASUREMENTS.value}
            if batch.batch_kind is MeasurementBatchKind.CORRECTION:
                allowed.add(ExperimentState.MEASURED.value)
            if experiment.state not in allowed:
                raise MeasurementIngestionError(
                    "measurements may be submitted only after completed human execution "
                    f"and before QC; experiment is {experiment.state!r}"
                )
            uow.measurement_ingestion.add_batch(batch)
            plan_record = uow.measurement_ingestion.latest_plan(experiment.id)
            plan = ExpectedMeasurementPlan.model_validate(plan_record.plan_snapshot, strict=False)
            active = {
                row.expected_measurement_id
                for row in uow.measurement_ingestion.active_revisions(experiment.id)
            }
            required = {row.expected_measurement_id for row in plan.entries if row.required}
            if not required.issubset(active):
                raise MeasurementIngestionError(
                    "measurement submission did not reconcile every required plan entry"
                )
            if experiment.state == ExperimentState.AWAITING_MEASUREMENTS.value:
                transition_experiment(
                    batch.experiment_id,
                    ExperimentState.AWAITING_MEASUREMENTS,
                    ExperimentState.MEASURED,
                    reason="complete external measurement plan reconciled",
                )
                uow.experiments.transition(
                    experiment.id,
                    ExperimentState.MEASURED,
                    expected_state=ExperimentState.AWAITING_MEASUREMENTS,
                    actor=batch.submitted_by,
                    reason="complete external measurement plan reconciled",
                    details={
                        "batch_id": str(batch.batch_id),
                        "source_type": batch.source_type.value,
                        "batch_sha256": batch.batch_sha256,
                        "explicitly_incomplete": batch.has_unavailable_values,
                    },
                )
            uow.commit()

    def status(self, experiment_id: UUID) -> dict[str, object]:
        with self.storage.unit_of_work() as uow:
            plan_record = uow.measurement_ingestion.latest_plan(str(experiment_id))
            plan = ExpectedMeasurementPlan.model_validate(plan_record.plan_snapshot, strict=False)
            active = {
                item.expected_measurement_id: item
                for item in uow.measurement_ingestion.active_revisions(str(experiment_id))
            }
            uow.commit()
        required = [item for item in plan.entries if item.required]
        available = sum(
            active.get(item.expected_measurement_id) is not None
            and active[item.expected_measurement_id].availability == "available"
            for item in required
        )
        unavailable = sum(
            active.get(item.expected_measurement_id) is not None
            and active[item.expected_measurement_id].availability == "unavailable"
            for item in required
        )
        return {
            "experiment_id": str(experiment_id),
            "plan_id": str(plan.plan_id),
            "plan_sha256": plan.plan_sha256,
            "expected": len(required),
            "received": available + unavailable,
            "available": available,
            "explicitly_unavailable": unavailable,
            "missing": len(required) - available - unavailable,
            "complete": available + unavailable == len(required),
        }
