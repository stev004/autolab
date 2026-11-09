"""Reconcile time-series readings against the expected sampling plan.

Each plan entry ends in exactly one status, so a missing reading is recorded as
evidence rather than silently dropped:

- ``matched``: a reading for this entry arrived inside its time window;
- ``out_of_window``: a reading arrived, but too early or too late to count;
- ``missing``: no reading (the request failed or was never made).

Readings that do not belong to any plan entry are reported as ``unexpected``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from autonomous_lab.culture.models import TimeSeriesMeasurementPlan
from autonomous_lab.domain.base import DomainModel
from autonomous_lab.hardware.sensors.contracts import MeasurementRequest, RawSensorMeasurement

EntryStatus = Literal["matched", "out_of_window", "missing"]


class SampleReading(DomainModel):
    """The runner's record of one sampling attempt: request plus outcome."""

    expected_measurement_id: str
    request: MeasurementRequest
    measurement: RawSensorMeasurement | None = None
    error_code: str | None = None


class ReconciledEntry(DomainModel):
    expected_measurement_id: str
    status: EntryStatus
    required: bool
    due_at: datetime
    measurement: RawSensorMeasurement | None = None
    error_code: str | None = None


class TimeSeriesReconciliation(DomainModel):
    plan_id: UUID
    entries: tuple[ReconciledEntry, ...]
    unexpected_measurement_ids: tuple[UUID, ...] = Field(default=())

    def matched(self) -> tuple[ReconciledEntry, ...]:
        return tuple(entry for entry in self.entries if entry.status == "matched")


def reconcile_time_series(
    plan: TimeSeriesMeasurementPlan,
    *,
    hold_started_at: datetime,
    readings: tuple[SampleReading, ...],
) -> TimeSeriesReconciliation:
    by_entry: dict[str, list[SampleReading]] = {}
    unexpected: list[UUID] = []
    known = {point.expected_measurement_id for point in plan.points}
    for reading in readings:
        if reading.expected_measurement_id not in known:
            if reading.measurement is not None:
                unexpected.append(reading.measurement.raw_measurement_id)
            continue
        by_entry.setdefault(reading.expected_measurement_id, []).append(reading)

    entries: list[ReconciledEntry] = []
    for point in plan.points:
        attempts = by_entry.get(point.expected_measurement_id, [])
        successful = [item for item in attempts if item.measurement is not None]
        # More than one successful reading for one entry is a duplicate: keep the
        # first as the entry's value and flag the rest so nothing is double counted.
        for extra in successful[1:]:
            assert extra.measurement is not None
            unexpected.append(extra.measurement.raw_measurement_id)
        due = point.due_at(hold_started_at)
        if not successful:
            error = next((item.error_code for item in attempts if item.error_code), None)
            entries.append(
                ReconciledEntry(
                    expected_measurement_id=point.expected_measurement_id,
                    status="missing",
                    required=point.required,
                    due_at=due,
                    error_code=error,
                )
            )
            continue
        measurement = successful[0].measurement
        assert measurement is not None
        opens, closes = point.window(hold_started_at)
        in_window = opens <= measurement.measured_at <= closes
        entries.append(
            ReconciledEntry(
                expected_measurement_id=point.expected_measurement_id,
                status="matched" if in_window else "out_of_window",
                required=point.required,
                due_at=due,
                measurement=measurement,
            )
        )
    return TimeSeriesReconciliation(
        plan_id=plan.plan_id,
        entries=tuple(entries),
        unexpected_measurement_ids=tuple(unexpected),
    )
