"""Minimal interface implemented by simulated and physical devices."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from autonomous_lab.domain.protocols import CapabilityManifest, ProtocolCommand
from autonomous_lab.hardware.commands import DeviceResponse, ExecutionContext


@runtime_checkable
class Device(Protocol):
    @property
    def device_id(self) -> str: ...

    async def capability_manifest(self) -> CapabilityManifest: ...

    async def execute(
        self,
        command: ProtocolCommand,
        context: ExecutionContext,
    ) -> DeviceResponse: ...

    async def enter_safe_state(self, reason: str) -> None: ...
