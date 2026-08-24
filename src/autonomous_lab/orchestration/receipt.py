"""Adapter that makes deterministic validation the execution authority."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from autonomous_lab.campaigns.config import ValidationPolicyConfig
from autonomous_lab.domain.protocols import ExecutableProtocol, ValidationReport
from autonomous_lab.hardware.capabilities import CapabilitySnapshot
from autonomous_lab.validation.validator import ProtocolValidationService


class ReceiptVerifier(Protocol):
    def verify(
        self,
        executable: ExecutableProtocol,
        current_capabilities: CapabilitySnapshot,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ValidationServiceReceiptVerifier:
    """Verify against a trusted report and current validation inputs.

    This class intentionally contains no second implementation of receipt
    checks: :class:`ProtocolValidationService` remains the sole authority both
    when issuing and verifying executable artifacts.
    """

    validation_service: ProtocolValidationService
    report_provider: Callable[[UUID], ValidationReport]
    policy: ValidationPolicyConfig

    @classmethod
    def from_reports(
        cls,
        reports: Mapping[UUID, ValidationReport],
        *,
        validation_service: ProtocolValidationService,
        policy: ValidationPolicyConfig,
    ) -> ValidationServiceReceiptVerifier:
        def provide(report_id: UUID) -> ValidationReport:
            try:
                return reports[report_id]
            except KeyError as exc:
                raise LookupError(f"validation report {report_id} is not trusted") from exc

        return cls(
            validation_service=validation_service,
            report_provider=provide,
            policy=policy,
        )

    def verify(
        self,
        executable: ExecutableProtocol,
        current_capabilities: CapabilitySnapshot,
    ) -> None:
        report = self.report_provider(executable.validation_report_id)
        self.validation_service.verify_executable(
            executable,
            report=report,
            policy=self.policy,
            capability_snapshot=current_capabilities,
        )
