"""Conversion of neutral integrity QC into stable domain artifacts."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from autonomous_lab.domain.enums import QCStatus
from autonomous_lab.domain.qc import OutlierAssessment, QCCheckResult, QCResult
from autonomous_lab.qc.engine import IntegrityQCReport


def _resolve_identifier(identifier: str, mapping: Mapping[str, UUID]) -> UUID:
    if identifier in mapping:
        return mapping[identifier]
    try:
        return UUID(identifier)
    except ValueError as error:
        raise ValueError(
            f"QC measurement identifier {identifier!r} is not a UUID and has no mapping"
        ) from error


def qc_result_from_integrity_report(
    report: IntegrityQCReport,
    *,
    experiment_id: UUID,
    run_id: UUID,
    measurement_id_map: Mapping[str, UUID] | None = None,
) -> QCResult:
    """Convert QC while retaining flags, thresholds, and no-exclusion semantics."""

    identifier_map = measurement_id_map or {}
    checks = tuple(
        QCCheckResult(
            check_name=check.code,
            check_version=report.method_version,
            passed=check.passed,
            message=check.message,
            flagged_measurement_ids=tuple(
                _resolve_identifier(identifier, identifier_map)
                for identifier in check.measurement_ids
            ),
            details=check.details,
        )
        for check in report.checks
    )
    outlier_assessment = None
    if report.outlier_flags:
        first = report.outlier_flags[0]
        outlier_assessment = OutlierAssessment(
            method=first.method,
            threshold=str(first.threshold),
            flagged_measurement_ids=tuple(
                _resolve_identifier(flag.measurement_id, identifier_map)
                for flag in report.outlier_flags
            ),
            metrics_recomputed_with_exclusions=any(
                flag.excluded_from_metrics for flag in report.outlier_flags
            ),
        )
    return QCResult(
        experiment_id=experiment_id,
        run_id=run_id,
        status=QCStatus.PASSED if report.passed else QCStatus.FAILED,
        checks=checks,
        outlier_assessment=outlier_assessment,
    )
