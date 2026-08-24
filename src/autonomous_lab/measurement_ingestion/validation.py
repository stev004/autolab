"""Deterministic reconciliation of submitted values with an expected plan."""

from __future__ import annotations

from collections import Counter

from autonomous_lab.measurement_ingestion.models import (
    ExpectedMeasurement,
    ExpectedMeasurementPlan,
    ImportIssue,
    MeasurementAvailability,
    MeasurementBatchKind,
    RawMeasurement,
)


def reconcile_measurements(
    plan: ExpectedMeasurementPlan,
    measurements: tuple[RawMeasurement, ...],
    *,
    batch_kind: MeasurementBatchKind,
) -> tuple[ImportIssue, ...]:
    """Return every deterministic issue; never discard an invalid row silently."""

    issues: list[ImportIssue] = []
    if not measurements:
        issues.append(
            ImportIssue(
                code="empty_batch",
                message="measurement batch must contain at least one row",
            )
        )
    expected_by_id = {item.expected_measurement_id: item for item in plan.entries}
    counts = Counter(item.expected_measurement_id for item in measurements)
    for identifier, count in counts.items():
        if count > 1:
            issues.append(
                ImportIssue(
                    code="duplicate_measurement",
                    message=f"expected measurement {identifier!r} occurs {count} times",
                    details={"expected_measurement_id": identifier, "count": count},
                )
            )

    received = set(counts)
    if batch_kind is MeasurementBatchKind.INITIAL:
        for expected in plan.entries:
            if expected.required and expected.expected_measurement_id not in received:
                issues.append(
                    ImportIssue(
                        code="missing_measurement",
                        message=f"required sample {expected.sample_id!r} is missing",
                        sample_id=expected.sample_id,
                        details={
                            "expected_measurement_id": expected.expected_measurement_id,
                            "measurement_type": expected.measurement_type,
                        },
                    )
                )

    for row in measurements:
        expected = expected_by_id.get(row.expected_measurement_id)
        if expected is None:
            issues.append(
                ImportIssue(
                    code="unexpected_measurement",
                    message=f"measurement ID {row.expected_measurement_id!r} is not in the plan",
                    sample_id=row.sample_id,
                )
            )
            continue
        issues.extend(_row_issues(expected, row))

    return tuple(issues)


def _row_issues(
    expected: ExpectedMeasurement,
    row: RawMeasurement,
) -> tuple[ImportIssue, ...]:
    issues: list[ImportIssue] = []
    fields = (
        ("sample_id", row.sample_id, expected.sample_id),
        ("measurement_type", row.measurement_type, expected.measurement_type),
        ("unit", row.unit, expected.unit),
    )
    for field, actual, wanted in fields:
        if actual != wanted:
            issues.append(
                ImportIssue(
                    code=f"incorrect_{field}",
                    message=f"{field} is {actual!r}; expected {wanted!r}",
                    field=field,
                    sample_id=row.sample_id,
                    details={"actual": actual, "expected": wanted},
                )
            )

    expected_nm = None if expected.wavelength is None else expected.wavelength.to("nanometer").value
    actual_nm = None if row.wavelength is None else row.wavelength.to("nanometer").value
    if actual_nm != expected_nm:
        issues.append(
            ImportIssue(
                code="incorrect_wavelength",
                message=f"wavelength is {actual_nm!s} nm; expected {expected_nm!s} nm",
                field="wavelength_nm",
                sample_id=row.sample_id,
                details={"actual_nm": actual_nm, "expected_nm": expected_nm},
            )
        )

    if row.availability is MeasurementAvailability.AVAILABLE:
        assert row.value is not None
        if expected.minimum_value is not None and row.value < expected.minimum_value:
            issues.append(
                ImportIssue(
                    code="value_below_minimum",
                    message=(
                        f"value {row.value} is below deterministic minimum {expected.minimum_value}"
                    ),
                    field="value",
                    sample_id=row.sample_id,
                )
            )
        if expected.maximum_value is not None and row.value > expected.maximum_value:
            issues.append(
                ImportIssue(
                    code="value_above_maximum",
                    message=(
                        f"value {row.value} is above deterministic maximum {expected.maximum_value}"
                    ),
                    field="value",
                    sample_id=row.sample_id,
                )
            )
    return tuple(issues)
