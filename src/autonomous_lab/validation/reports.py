"""Validation results and receipt-verification failures."""

from __future__ import annotations

from dataclasses import dataclass

from autonomous_lab.domain.protocols import (
    ExecutableProtocol,
    ExperimentProtocol,
    ValidationIssue,
    ValidationReport,
)


@dataclass(frozen=True, slots=True)
class ValidationOutcome:
    """The report is always present; executable is present only after a clean pass."""

    protocol: ExperimentProtocol
    report: ValidationReport
    executable: ExecutableProtocol | None

    @property
    def passed(self) -> bool:
        return self.executable is not None

    def require_executable(self) -> ExecutableProtocol:
        if self.executable is None:
            codes = ", ".join(issue.code for issue in self.report.issues)
            raise ValueError(f"protocol was rejected by validation: {codes}")
        return self.executable


class ReceiptVerificationError(ValueError):
    """Raised when execution inputs no longer match their validation receipt."""

    def __init__(self, issues: tuple[ValidationIssue, ...]) -> None:
        self.issues = issues
        codes = ", ".join(issue.code for issue in issues)
        super().__init__(f"executable protocol receipt is invalid: {codes}")
