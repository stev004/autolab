from __future__ import annotations

import asyncio
import io
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from autonomous_lab.domain.enums import MeasurementRole
from autonomous_lab.domain.quantities import QuantityValue
from autonomous_lab.measurement_ingestion import (
    CSVMeasurementSource,
    ExpectedMeasurement,
    ManualBatchBuilder,
    ManualMeasurementSource,
    MeasurementBatchKind,
    RawMeasurementBatch,
    acquisition_context,
    make_expected_plan,
    parse_measurement_csv,
)
from autonomous_lab.measurement_ingestion.csv_import import read_measurement_csv

EXPERIMENT_ID = UUID("00000000-0000-0000-0000-000000000101")
PROTOCOL_ID = UUID("00000000-0000-0000-0000-000000000102")
NOW = datetime(2026, 8, 14, 18, 42, tzinfo=UTC)


def _plan():
    return make_expected_plan(
        experiment_id=EXPERIMENT_ID,
        protocol_id=PROTOCOL_ID,
        protocol_hash="a" * 64,
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
                minimum_value=Decimal("-0.1"),
                maximum_value=Decimal("3"),
            )
            for index, sample in enumerate(("B01", "B02"))
        ),
        created_at=NOW,
    )


def test_manual_builder_allows_draft_correction_then_finalizes_complete_batch() -> None:
    plan = _plan()
    builder = ManualBatchBuilder(plan, "operator-1", submitted_at=NOW)
    builder.set_value("measure:B01", Decimal("0.31"), measured_at=NOW)
    builder.set_value("measure:B01", Decimal("0.031"), measured_at=NOW, note="draft fix")
    builder.set_value("measure:B02", Decimal("0.029"), measured_at=NOW)

    result = builder.finalize()

    assert result.accepted
    assert result.batch is not None
    assert result.batch.measurements[0].value == Decimal("0.031")
    assert result.batch.source_type.value == "manual_entry"
    assert len(result.batch.batch_sha256) == 64


def test_manual_builder_reports_missing_and_accepts_explicit_unavailable() -> None:
    plan = _plan()
    builder = ManualBatchBuilder(plan, "operator", submitted_at=NOW)
    builder.set_value("measure:B01", Decimal("0.031"), measured_at=NOW)
    missing = builder.finalize()
    assert not missing.accepted
    assert "missing_measurement" in {issue.code for issue in missing.issues}

    builder.set_unavailable("measure:B02", "cuvette spilled", measured_at=NOW)
    reconciled = builder.finalize()
    assert reconciled.accepted
    assert reconciled.batch is not None
    assert reconciled.batch.has_unavailable_values


def test_canonical_csv_is_accepted_with_raw_file_hash() -> None:
    content = (
        b"sample_id,measurement_type,wavelength_nm,value,unit,timestamp\n"
        b"B01,absorbance,520,0.031,AU,2026-08-14T18:42:00Z\n"
        b"B02,absorbance,520,0.029,AU,2026-08-14T18:42:20Z\n"
    )
    result = parse_measurement_csv(
        content,
        plan=_plan(),
        submitted_by="operator-1",
        source_filename="readings.csv",
        submitted_at=NOW,
    )

    assert result.accepted
    assert result.batch is not None
    assert result.source_file_sha256 == result.batch.source_file_sha256
    assert result.batch.measurements[1].measured_at.second == 20


@pytest.mark.parametrize(
    ("rows", "expected_codes"),
    [
        (
            "B01,absorbance,520,0.031,AU,2026-08-14T18:42:00Z\n"
            "B01,absorbance,520,0.029,AU,2026-08-14T18:42:20Z\n",
            {"duplicate_measurement", "missing_measurement"},
        ),
        (
            "X01,absorbance,520,0.031,AU,2026-08-14T18:42:00Z\n"
            "B02,absorbance,520,0.029,AU,2026-08-14T18:42:20Z\n",
            {"unexpected_sample_id", "missing_measurement"},
        ),
        (
            "B01,absorbance,520,not-a-number,AU,2026-08-14T18:42:00Z\n"
            "B02,absorbance,520,0.029,AU,2026-08-14T18:42:20Z\n",
            {"malformed_value", "missing_measurement"},
        ),
        (
            "B01,absorbance,520,0.031,dimensionless,2026-08-14T18:42:00Z\n"
            "B02,absorbance,520,0.029,AU,2026-08-14T18:42:20Z\n",
            {"incorrect_unit", "missing_measurement"},
        ),
        (
            "B01,absorbance,600,0.031,AU,2026-08-14T18:42:00Z\n"
            "B02,absorbance,520,0.029,AU,2026-08-14T18:42:20Z\n",
            {"incorrect_wavelength", "missing_measurement"},
        ),
    ],
)
def test_csv_rejects_entire_file_with_structured_issues(
    rows: str, expected_codes: set[str]
) -> None:
    content = ("sample_id,measurement_type,wavelength_nm,value,unit,timestamp\n" + rows).encode()
    result = parse_measurement_csv(
        content,
        plan=_plan(),
        submitted_by="operator",
        source_filename="bad.csv",
    )
    assert not result.accepted
    assert result.batch is None
    assert expected_codes <= {issue.code for issue in result.issues}


def test_extended_csv_can_explicitly_mark_a_value_unavailable() -> None:
    content = (
        b"sample_id,measurement_type,wavelength_nm,value,unit,timestamp,status,notes\n"
        b"B01,absorbance,520,0.031,AU,2026-08-14T18:42:00Z,available,\n"
        b"B02,absorbance,520,,AU,2026-08-14T18:42:20Z,unavailable,sample spilled\n"
    )
    result = parse_measurement_csv(
        content,
        plan=_plan(),
        submitted_by="operator",
        source_filename="incomplete.csv",
    )
    assert result.accepted
    assert result.batch is not None
    assert result.batch.has_unavailable_values


def test_hash_bound_batch_rejects_tampering() -> None:
    builder = ManualBatchBuilder(_plan(), "operator", submitted_at=NOW)
    builder.set_value("measure:B01", Decimal("0.031"), measured_at=NOW)
    builder.set_value("measure:B02", Decimal("0.029"), measured_at=NOW)
    batch = builder.finalize().batch
    assert batch is not None
    payload = batch.model_dump(mode="python")
    payload["submitted_by"] = "someone-else"
    with pytest.raises(ValidationError, match="batch_sha256"):
        RawMeasurementBatch.model_validate(payload)


def test_correction_batch_requires_reason() -> None:
    builder = ManualBatchBuilder(
        _plan(),
        "operator",
        batch_kind=MeasurementBatchKind.CORRECTION,
        submitted_at=NOW,
    )
    builder.set_value("measure:B01", Decimal("0.032"), measured_at=NOW)
    result = builder.finalize()
    assert not result.accepted
    assert "missing_correction_reason" in {issue.code for issue in result.issues}


def test_manual_and_csv_sources_implement_same_async_boundary() -> None:
    plan = _plan()
    builder = ManualBatchBuilder(plan, "operator", submitted_at=NOW)
    builder.set_value("measure:B01", Decimal("0.031"), measured_at=NOW)
    builder.set_value("measure:B02", Decimal("0.029"), measured_at=NOW)
    manual_batch = builder.finalize().batch
    assert manual_batch is not None
    context = acquisition_context(
        experiment_id=plan.experiment_id,
        run_id=UUID("00000000-0000-0000-0000-000000000109"),
        submitted_by="operator",
        acquired_at=NOW,
    )
    assert asyncio.run(ManualMeasurementSource(manual_batch).acquire(plan, context)) is manual_batch

    content = (
        b"sample_id,measurement_type,wavelength_nm,value,unit,timestamp\n"
        b"B01,absorbance,520,0.031,AU,2026-08-14T18:42:00Z\n"
        b"B02,absorbance,520,0.029,AU,2026-08-14T18:42:20Z\n"
    )
    csv_batch = asyncio.run(
        CSVMeasurementSource(content, source_filename="readings.csv").acquire(plan, context)
    )
    assert csv_batch.source_type.value == "file_import"
    assert csv_batch.source_file_sha256 is not None


def test_oversized_bytes_are_rejected_before_hashing_or_decoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import autonomous_lab.measurement_ingestion.csv_import as importer

    monkeypatch.setattr(importer, "MAX_CSV_BYTES", 32)
    result = parse_measurement_csv(
        b"\xff" * 33, plan=_plan(), submitted_by="operator", source_filename="large.csv"
    )
    assert not result.accepted
    assert result.source_file_sha256 is None
    assert [issue.code for issue in result.issues] == ["file_too_large"]


def test_file_reader_bounds_the_read_without_trusting_file_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import autonomous_lab.measurement_ingestion.csv_import as importer

    sizes: list[int] = []

    class RecordingStream(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            sizes.append(size)
            return super().read(size)

    monkeypatch.setattr(importer, "MAX_CSV_BYTES", 32)
    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: RecordingStream(b"x" * 100))
    with pytest.raises(ValueError, match="file_too_large"):
        read_measurement_csv("growing.csv")
    assert sizes == [33]


def test_row_limit_stops_before_later_malformed_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import autonomous_lab.measurement_ingestion.csv_import as importer

    monkeypatch.setattr(importer, "MAX_CSV_ROWS", 2)
    content = (
        b"sample_id,measurement_type,wavelength_nm,value,unit,timestamp\n"
        + b"B01,absorbance,520,0.031,AU,2026-08-14T18:42:00Z\n" * 3
        + b'"unterminated later row'
    )
    result = parse_measurement_csv(
        content, plan=_plan(), submitted_by="operator", source_filename="rows.csv"
    )
    assert [issue.code for issue in result.issues] == ["too_many_rows"]
    assert result.batch is None


def test_invalid_row_diagnostics_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    import autonomous_lab.measurement_ingestion.csv_import as importer

    monkeypatch.setattr(importer, "MAX_CSV_ISSUES", 2)
    content = (
        b"sample_id,measurement_type,wavelength_nm,value,unit,timestamp\n"
        + b"unexpected,absorbance,520,1,AU,2026-08-14T18:42:00Z\n" * 10
    )
    result = parse_measurement_csv(
        content, plan=_plan(), submitted_by="operator", source_filename="bad.csv"
    )
    assert [issue.code for issue in result.issues] == ["too_many_issues"]


@pytest.mark.parametrize("value", ["1e1000000", "1e-100000", "0e1000000", "1" * 129])
def test_csv_extreme_numbers_return_issues_instead_of_expanding_or_overflowing(value: str) -> None:
    content = (
        "sample_id,measurement_type,wavelength_nm,value,unit,timestamp\n"
        f"B01,absorbance,520,{value},AU,2026-08-14T18:42:00Z\n"
        "B02,absorbance,520,0.029,AU,2026-08-14T18:42:00Z\n"
    ).encode()
    result = parse_measurement_csv(
        content, plan=_plan(), submitted_by="operator", source_filename="exponents.csv"
    )
    assert not result.accepted
    assert "malformed_value" in {issue.code for issue in result.issues}


def test_manual_entry_rejects_extreme_exponents_but_preserves_precision() -> None:
    builder = ManualBatchBuilder(_plan(), "operator", submitted_at=NOW)
    with pytest.raises(ValidationError, match="adjusted exponent"):
        builder.set_value("measure:B01", Decimal("1e-100000"), measured_at=NOW)
    precise = Decimal("0.03112345678901234567890123456789")
    builder.set_value("measure:B01", precise, measured_at=NOW)
    builder.set_value("measure:B02", Decimal("0.029"), measured_at=NOW)
    batch = builder.finalize().batch
    assert batch is not None
    assert batch.measurements[0].value == precise
