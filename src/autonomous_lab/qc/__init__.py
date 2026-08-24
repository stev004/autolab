"""Run-integrity QC, deliberately separate from scientific performance."""

from autonomous_lab.qc.checks import (
    ConstraintEvaluation,
    MetricConstraint,
    evaluate_performance_constraints,
)
from autonomous_lab.qc.domain_adapters import qc_result_from_integrity_report
from autonomous_lab.qc.engine import (
    IntegrityQCConfig,
    IntegrityQCReport,
    QCCheck,
    evaluate_integrity_qc,
)
from autonomous_lab.qc.outliers import OutlierFlag, flag_modified_z_score_outliers

__all__ = [
    "ConstraintEvaluation",
    "IntegrityQCConfig",
    "IntegrityQCReport",
    "MetricConstraint",
    "OutlierFlag",
    "QCCheck",
    "evaluate_integrity_qc",
    "evaluate_performance_constraints",
    "flag_modified_z_score_outliers",
    "qc_result_from_integrity_report",
]
