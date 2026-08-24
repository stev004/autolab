"""Central retry policy; writes are never automatically replayed."""

from dataclasses import dataclass

from autonomous_lab.domain.protocols import MeasureAbsorbanceCommand, ProtocolStep
from autonomous_lab.hardware.errors import DeviceExecutionError


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    maximum_read_attempts: int = 2

    def __post_init__(self) -> None:
        if self.maximum_read_attempts < 1:
            raise ValueError("maximum_read_attempts must be at least one")

    def should_retry(
        self,
        step: ProtocolStep,
        error: DeviceExecutionError,
        *,
        attempt: int,
    ) -> bool:
        return (
            isinstance(step.command, MeasureAbsorbanceCommand)
            and step.retry_safe
            and error.retry_safe
            and not error.ambiguous
            and attempt < self.maximum_read_attempts
        )
