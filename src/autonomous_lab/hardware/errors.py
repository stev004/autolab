"""Typed hardware failures and their recovery semantics."""

from __future__ import annotations


class DeviceCapabilityError(RuntimeError):
    """No unambiguous registered device can execute a requested command."""


class DeviceExecutionError(RuntimeError):
    """A device command failed with explicit retry and state semantics."""

    def __init__(
        self,
        category: str,
        message: str | None = None,
        *,
        retry_safe: bool = False,
        ambiguous: bool = False,
    ) -> None:
        super().__init__(message or category)
        self.category = category
        self.retry_safe = retry_safe
        self.ambiguous = ambiguous


class DeviceTimeoutError(DeviceExecutionError):
    def __init__(self, message: str = "device command timed out") -> None:
        super().__init__("timeout", message, retry_safe=True)


class AmbiguousDeviceStateError(DeviceExecutionError):
    """The physical effect may have happened, so automatic replay is unsafe."""

    def __init__(self, category: str, message: str | None = None) -> None:
        super().__init__(category, message, retry_safe=False, ambiguous=True)
