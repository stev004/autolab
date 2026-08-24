"""Programmatic manual-entry workflow with editable drafts and immutable final batches."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from autonomous_lab.domain.identifiers import utc_now
from autonomous_lab.measurement_ingestion.models import (
    ExpectedMeasurementPlan,
    ImportIssue,
    MeasurementAvailability,
    MeasurementBatchKind,
    MeasurementImportResult,
    MeasurementSourceType,
    RawMeasurement,
    make_raw_batch,
)
from autonomous_lab.measurement_ingestion.validation import reconcile_measurements


class ManualBatchBuilder:
    """Mutable draft used only until a manual submission is finalized.

    Calling ``set_value`` again corrects the draft before submission. Once
    ``finalize`` returns a batch, that immutable batch must be persisted; later
    changes use a new ``CORRECTION`` batch.
    """

    def __init__(
        self,
        plan: ExpectedMeasurementPlan,
        operator_id: str,
        *,
        batch_kind: MeasurementBatchKind = MeasurementBatchKind.INITIAL,
        submitted_at: datetime | None = None,
        notes: str | None = None,
    ) -> None:
        if not operator_id.strip():
            raise ValueError("operator_id must not be blank")
        self.plan = plan
        self.operator_id = operator_id
        self.batch_kind = batch_kind
        self.submitted_at = submitted_at or utc_now()
        self.notes = notes
        self._rows: dict[str, RawMeasurement] = {}

    def set_value(
        self,
        expected_measurement_id: str,
        value: Decimal | int | float,
        *,
        measured_at: datetime,
        unit: str | None = None,
        note: str | None = None,
        correction_reason: str | None = None,
        source_metadata: dict[str, Any] | None = None,
    ) -> None:
        expected = self._expected(expected_measurement_id)
        self._rows[expected_measurement_id] = RawMeasurement(
            expected_measurement_id=expected_measurement_id,
            sample_id=expected.sample_id,
            measurement_type=expected.measurement_type,
            wavelength=expected.wavelength,
            availability=MeasurementAvailability.AVAILABLE,
            value=Decimal(str(value)),
            unit=unit or expected.unit,
            measured_at=measured_at,
            note=note,
            correction_reason=correction_reason,
            source_metadata=source_metadata or {},
        )

    def set_unavailable(
        self,
        expected_measurement_id: str,
        reason: str,
        *,
        measured_at: datetime,
        note: str | None = None,
        correction_reason: str | None = None,
        source_metadata: dict[str, Any] | None = None,
    ) -> None:
        expected = self._expected(expected_measurement_id)
        self._rows[expected_measurement_id] = RawMeasurement(
            expected_measurement_id=expected_measurement_id,
            sample_id=expected.sample_id,
            measurement_type=expected.measurement_type,
            wavelength=expected.wavelength,
            availability=MeasurementAvailability.UNAVAILABLE,
            value=None,
            unit=expected.unit,
            measured_at=measured_at,
            note=note,
            unavailable_reason=reason,
            correction_reason=correction_reason,
            source_metadata=source_metadata or {},
        )

    def remove(self, expected_measurement_id: str) -> None:
        self._rows.pop(expected_measurement_id, None)

    def preview(self) -> tuple[RawMeasurement, ...]:
        order = {
            item.expected_measurement_id: index for index, item in enumerate(self.plan.entries)
        }
        return tuple(
            sorted(self._rows.values(), key=lambda row: order[row.expected_measurement_id])
        )

    def finalize(self) -> MeasurementImportResult:
        rows = self.preview()
        issues = reconcile_measurements(self.plan, rows, batch_kind=self.batch_kind)
        issues += self._correction_issues(rows)
        if issues:
            return MeasurementImportResult(plan_id=self.plan.plan_id, issues=issues)
        batch = make_raw_batch(
            experiment_id=self.plan.experiment_id,
            plan_id=self.plan.plan_id,
            source_type=MeasurementSourceType.MANUAL_ENTRY,
            batch_kind=self.batch_kind,
            measurements=rows,
            submitted_by=self.operator_id,
            submitted_at=self.submitted_at,
            notes=self.notes,
        )
        return MeasurementImportResult(plan_id=self.plan.plan_id, batch=batch)

    def _expected(self, identifier: str):  # type annotation avoids a duplicate import
        for expected in self.plan.entries:
            if expected.expected_measurement_id == identifier:
                return expected
        raise KeyError(f"expected measurement {identifier!r} is not in the plan")

    def _correction_issues(self, rows: tuple[RawMeasurement, ...]) -> tuple[ImportIssue, ...]:
        if self.batch_kind is not MeasurementBatchKind.CORRECTION:
            return ()
        return tuple(
            ImportIssue(
                code="missing_correction_reason",
                message="a corrected measurement requires a correction reason",
                sample_id=row.sample_id,
                details={"expected_measurement_id": row.expected_measurement_id},
            )
            for row in rows
            if not row.correction_reason
        )


def build_manual_batch(
    plan: ExpectedMeasurementPlan,
    operator_id: str,
    measurements: tuple[RawMeasurement, ...],
    *,
    batch_kind: MeasurementBatchKind = MeasurementBatchKind.INITIAL,
    submitted_at: datetime | None = None,
    notes: str | None = None,
) -> MeasurementImportResult:
    """Validate already-collected manual rows without an interactive builder."""

    issues = reconcile_measurements(plan, measurements, batch_kind=batch_kind)
    if batch_kind is MeasurementBatchKind.CORRECTION:
        issues += tuple(
            ImportIssue(
                code="missing_correction_reason",
                message="a corrected measurement requires a correction reason",
                sample_id=row.sample_id,
            )
            for row in measurements
            if not row.correction_reason
        )
    if issues:
        return MeasurementImportResult(plan_id=plan.plan_id, issues=issues)
    batch = make_raw_batch(
        experiment_id=plan.experiment_id,
        plan_id=plan.plan_id,
        source_type=MeasurementSourceType.MANUAL_ENTRY,
        measurements=measurements,
        submitted_by=operator_id,
        batch_kind=batch_kind,
        submitted_at=submitted_at,
        notes=notes,
    )
    return MeasurementImportResult(plan_id=plan.plan_id, batch=batch)
