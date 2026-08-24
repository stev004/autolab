"""Strict, all-or-nothing parser for AutoLab's canonical measurement CSV."""

from __future__ import annotations

import csv
import hashlib
import io
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import cast

from pydantic import ValidationError

from autonomous_lab.domain.enums import MeasurementSourceType
from autonomous_lab.domain.quantities import QuantityValue
from autonomous_lab.measurement_ingestion.models import (
    ExpectedMeasurement,
    ExpectedMeasurementPlan,
    ImportIssue,
    MeasurementAvailability,
    MeasurementBatchKind,
    MeasurementImportResult,
    RawMeasurement,
    make_raw_batch,
)
from autonomous_lab.measurement_ingestion.validation import reconcile_measurements

_BASE_HEADER = (
    "sample_id",
    "measurement_type",
    "wavelength_nm",
    "value",
    "unit",
    "timestamp",
)
_EXTENDED_HEADER = (*_BASE_HEADER, "status", "notes")


def parse_measurement_csv(
    content: bytes,
    *,
    plan: ExpectedMeasurementPlan,
    submitted_by: str,
    source_filename: str,
    batch_kind: MeasurementBatchKind = MeasurementBatchKind.INITIAL,
    correction_reason: str | None = None,
    submitted_at: datetime | None = None,
) -> MeasurementImportResult:
    """Parse canonical CSV bytes without accepting a partially invalid file.

    The accepted headers are the documented six-column format and an extended
    form adding ``status`` and ``notes``. Explicit unavailable values require
    the extended form with ``status=unavailable`` and a reason in ``notes``.
    """

    source_hash = hashlib.sha256(content).hexdigest()
    issues: list[ImportIssue] = []
    try:
        text = content.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        return _failed(
            plan,
            source_hash,
            ImportIssue(code="invalid_encoding", message=f"CSV must be UTF-8: {exc}"),
        )
    if "\x00" in text:
        return _failed(
            plan,
            source_hash,
            ImportIssue(code="nul_byte", message="CSV cannot contain NUL bytes"),
        )

    try:
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        header = tuple(reader.fieldnames or ())
        if header not in {_BASE_HEADER, _EXTENDED_HEADER}:
            return _failed(
                plan,
                source_hash,
                ImportIssue(
                    code="invalid_header",
                    message=(
                        "CSV header must exactly match the canonical six-column or "
                        "extended eight-column format"
                    ),
                    details={"actual": header, "accepted": (_BASE_HEADER, _EXTENDED_HEADER)},
                ),
            )
        raw_rows = list(reader)
    except csv.Error as exc:
        return _failed(
            plan,
            source_hash,
            ImportIssue(code="malformed_csv", message=f"malformed CSV: {exc}"),
        )

    expected_by_sample: dict[str, list[ExpectedMeasurement]] = {}
    for expected in plan.entries:
        expected_by_sample.setdefault(expected.sample_id, []).append(expected)

    parsed: list[RawMeasurement] = []
    seen_expected: set[str] = set()
    for row_number, untyped_row in enumerate(raw_rows, start=2):
        if None in untyped_row or any(value is None for value in untyped_row.values()):
            issues.append(
                ImportIssue(
                    code="malformed_row",
                    message="row has too many or too few columns",
                    row_number=row_number,
                )
            )
            continue
        row = cast(dict[str, str], untyped_row)
        sample_id = row["sample_id"].strip()
        measurement_type = row["measurement_type"].strip()
        candidates = expected_by_sample.get(sample_id, [])
        if not candidates:
            issues.append(
                ImportIssue(
                    code="unexpected_sample_id",
                    message=f"sample {sample_id!r} is not present in the measurement plan",
                    row_number=row_number,
                    field="sample_id",
                    sample_id=sample_id,
                )
            )
            continue
        typed = [item for item in candidates if item.measurement_type == measurement_type]
        if not typed:
            issues.append(
                ImportIssue(
                    code="unexpected_measurement_type",
                    message=(
                        f"measurement type {measurement_type!r} is not expected for {sample_id!r}"
                    ),
                    row_number=row_number,
                    field="measurement_type",
                    sample_id=sample_id,
                    details={"expected": [item.measurement_type for item in candidates]},
                )
            )
            continue

        wavelength, wavelength_issue = _parse_wavelength(row["wavelength_nm"], row_number)
        if wavelength_issue is not None:
            issues.append(wavelength_issue)
            continue
        matches = [item for item in typed if _same_wavelength(item, wavelength)]
        if len(matches) != 1:
            issues.append(
                ImportIssue(
                    code="incorrect_wavelength",
                    message=f"wavelength is not expected for sample {sample_id!r}",
                    row_number=row_number,
                    field="wavelength_nm",
                    sample_id=sample_id,
                    details={
                        "expected_nm": [
                            None
                            if item.wavelength is None
                            else str(item.wavelength.to("nanometer").value)
                            for item in typed
                        ]
                    },
                )
            )
            continue
        expected = matches[0]
        if expected.expected_measurement_id in seen_expected:
            issues.append(
                ImportIssue(
                    code="duplicate_measurement",
                    message=f"sample {sample_id!r} is duplicated",
                    row_number=row_number,
                    sample_id=sample_id,
                    details={"expected_measurement_id": expected.expected_measurement_id},
                )
            )
            continue
        seen_expected.add(expected.expected_measurement_id)

        raw, row_issues = _parse_row(
            row,
            expected=expected,
            wavelength=wavelength,
            row_number=row_number,
            batch_kind=batch_kind,
            correction_reason=correction_reason,
        )
        issues.extend(row_issues)
        if raw is not None:
            parsed.append(raw)

    measurements = tuple(parsed)
    issues.extend(reconcile_measurements(plan, measurements, batch_kind=batch_kind))
    if issues:
        return MeasurementImportResult(
            plan_id=plan.plan_id,
            source_file_sha256=source_hash,
            issues=tuple(issues),
        )
    batch = make_raw_batch(
        experiment_id=plan.experiment_id,
        plan_id=plan.plan_id,
        source_type=MeasurementSourceType.FILE_IMPORT,
        batch_kind=batch_kind,
        measurements=measurements,
        submitted_by=submitted_by,
        submitted_at=submitted_at,
        source_file_sha256=source_hash,
        source_filename=source_filename,
    )
    return MeasurementImportResult(
        plan_id=plan.plan_id,
        source_file_sha256=source_hash,
        batch=batch,
    )


def _parse_row(
    row: dict[str, str],
    *,
    expected: ExpectedMeasurement,
    wavelength: QuantityValue | None,
    row_number: int,
    batch_kind: MeasurementBatchKind,
    correction_reason: str | None,
) -> tuple[RawMeasurement | None, tuple[ImportIssue, ...]]:
    issues: list[ImportIssue] = []
    status_text = row.get("status", "available").strip().lower() or "available"
    try:
        availability = MeasurementAvailability(status_text)
    except ValueError:
        issues.append(
            ImportIssue(
                code="invalid_status",
                message="status must be 'available' or 'unavailable'",
                row_number=row_number,
                field="status",
                sample_id=expected.sample_id,
            )
        )
        availability = MeasurementAvailability.AVAILABLE

    value: Decimal | None = None
    value_text = row["value"].strip()
    if availability is MeasurementAvailability.AVAILABLE:
        try:
            value = Decimal(value_text)
            if not value.is_finite():
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            issues.append(
                ImportIssue(
                    code="malformed_value",
                    message=f"value {value_text!r} is not a finite number",
                    row_number=row_number,
                    field="value",
                    sample_id=expected.sample_id,
                )
            )
    elif value_text:
        issues.append(
            ImportIssue(
                code="value_for_unavailable_measurement",
                message="an unavailable measurement must have an empty value",
                row_number=row_number,
                field="value",
                sample_id=expected.sample_id,
            )
        )

    timestamp: datetime | None = None
    try:
        timestamp = _parse_utc_timestamp(row["timestamp"].strip())
    except ValueError as exc:
        issues.append(
            ImportIssue(
                code="invalid_timestamp",
                message=str(exc),
                row_number=row_number,
                field="timestamp",
                sample_id=expected.sample_id,
            )
        )

    note = row.get("notes", "").strip() or None
    unit = row["unit"].strip()
    if unit != expected.unit:
        issues.append(
            ImportIssue(
                code="incorrect_unit",
                message=f"unit is {unit!r}; expected {expected.unit!r}",
                row_number=row_number,
                field="unit",
                sample_id=expected.sample_id,
            )
        )
    if batch_kind is MeasurementBatchKind.CORRECTION and not correction_reason:
        issues.append(
            ImportIssue(
                code="missing_correction_reason",
                message="correction imports require a correction reason",
                row_number=row_number,
                sample_id=expected.sample_id,
            )
        )
    if availability is MeasurementAvailability.UNAVAILABLE and not note:
        issues.append(
            ImportIssue(
                code="missing_unavailable_reason",
                message="an unavailable value requires its reason in notes",
                row_number=row_number,
                field="notes",
                sample_id=expected.sample_id,
            )
        )
    if issues or timestamp is None:
        return None, tuple(issues)
    try:
        raw = RawMeasurement(
            expected_measurement_id=expected.expected_measurement_id,
            sample_id=expected.sample_id,
            measurement_type=expected.measurement_type,
            wavelength=wavelength,
            availability=availability,
            value=value,
            unit=unit,
            measured_at=timestamp,
            note=note,
            unavailable_reason=(
                note if availability is MeasurementAvailability.UNAVAILABLE else None
            ),
            correction_reason=correction_reason,
            source_metadata={"csv_row_number": row_number},
        )
    except ValidationError as exc:
        return None, (
            ImportIssue(
                code="invalid_row",
                message=str(exc),
                row_number=row_number,
                sample_id=expected.sample_id,
            ),
        )
    return raw, ()


def _parse_wavelength(
    text: str, row_number: int
) -> tuple[QuantityValue | None, ImportIssue | None]:
    value = text.strip()
    if not value:
        return None, None
    try:
        wavelength = Decimal(value)
        if not wavelength.is_finite() or wavelength <= 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        return None, ImportIssue(
            code="malformed_wavelength",
            message=f"wavelength {value!r} must be a positive finite number",
            row_number=row_number,
            field="wavelength_nm",
        )
    return QuantityValue(value=wavelength, unit="nanometer"), None


def _same_wavelength(expected: ExpectedMeasurement, actual: QuantityValue | None) -> bool:
    if expected.wavelength is None or actual is None:
        return expected.wavelength is actual
    return expected.wavelength.to("nanometer").value == actual.to("nanometer").value


def _parse_utc_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"timestamp {value!r} is not valid ISO 8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError("measurement timestamp must include UTC timezone (Z or +00:00)")
    return parsed


def _failed(
    plan: ExpectedMeasurementPlan,
    source_hash: str,
    issue: ImportIssue,
) -> MeasurementImportResult:
    return MeasurementImportResult(
        plan_id=plan.plan_id,
        source_file_sha256=source_hash,
        issues=(issue,),
    )
