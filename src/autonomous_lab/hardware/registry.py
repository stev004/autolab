"""Device discovery and deterministic command routing."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from autonomous_lab.domain.enums import CommandType
from autonomous_lab.domain.protocols import CapabilityManifest, ProtocolStep
from autonomous_lab.hardware.capabilities import CapabilitySnapshot
from autonomous_lab.hardware.errors import DeviceCapabilityError
from autonomous_lab.hardware.interfaces import Device


@dataclass(frozen=True, slots=True)
class _RegisteredDevice:
    device: Device
    manifest: CapabilityManifest


class DeviceRegistry:
    """Frozen view of devices and capabilities for one execution boundary."""

    def __init__(self, registered: tuple[_RegisteredDevice, ...]) -> None:
        if not registered:
            raise ValueError("at least one device must be registered")
        identifiers = [entry.manifest.device_id for entry in registered]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("device IDs must be unique")
        self._registered = registered
        by_command: dict[CommandType, list[_RegisteredDevice]] = defaultdict(list)
        for entry in registered:
            for command_type in entry.manifest.supported_commands:
                by_command[command_type].append(entry)
        self._by_command = {key: tuple(value) for key, value in by_command.items()}

    @classmethod
    async def create(cls, devices: Iterable[Device]) -> DeviceRegistry:
        registered: list[_RegisteredDevice] = []
        for device in devices:
            manifest = await device.capability_manifest()
            if device.device_id != manifest.device_id:
                raise ValueError(
                    f"device {device.device_id!r} returned manifest for {manifest.device_id!r}"
                )
            registered.append(_RegisteredDevice(device=device, manifest=manifest))
        registered.sort(key=lambda item: item.manifest.device_id)
        return cls(tuple(registered))

    @property
    def snapshot(self) -> CapabilitySnapshot:
        return CapabilitySnapshot(manifests=tuple(item.manifest for item in self._registered))

    @property
    def capability_snapshot_hash(self) -> str:
        return self.snapshot.sha256

    def resolve(self, step: ProtocolStep) -> Device:
        candidates: tuple[_RegisteredDevice, ...] = self._by_command.get(
            step.command.command_type, ()
        )
        matches = tuple(
            entry
            for entry in candidates
            if step.required_capability
            in {
                entry.manifest.device_id,
                entry.manifest.device_type,
                step.command.command_type.value,
            }
        )
        if not matches:
            raise DeviceCapabilityError(
                f"no device provides {step.required_capability!r} for "
                f"{step.command.command_type.value!r}"
            )
        if len(matches) > 1:
            identifiers = sorted(entry.manifest.device_id for entry in matches)
            raise DeviceCapabilityError(
                f"capability {step.required_capability!r} is ambiguous across {identifiers!r}"
            )
        match = next(iter(matches))
        return match.device

    async def enter_safe_state(
        self,
        reason: str,
        *,
        timeout_seconds: float = 5.0,
    ) -> bool:
        """Ask every device to stop, attempting all devices even after a failure."""

        if timeout_seconds <= 0:
            raise ValueError("safe-state timeout must be positive")

        async def stop(entry: _RegisteredDevice) -> None:
            await asyncio.wait_for(
                entry.device.enter_safe_state(reason),
                timeout=timeout_seconds,
            )

        results = await asyncio.gather(
            *(stop(entry) for entry in reversed(self._registered)),
            return_exceptions=True,
        )
        return all(not isinstance(result, BaseException) for result in results)
