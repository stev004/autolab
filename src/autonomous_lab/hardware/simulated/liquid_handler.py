"""Manufacturer-neutral simulated liquid handler."""

from __future__ import annotations

from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from autonomous_lab.domain.enums import CommandType
from autonomous_lab.domain.identifiers import utc_now
from autonomous_lab.domain.protocols import (
    AspirateCommand,
    CapabilityManifest,
    DispenseCommand,
    MixCommand,
    ProtocolCommand,
    QuantityRange,
    TransferCommand,
)
from autonomous_lab.domain.quantities import QuantityValue
from autonomous_lab.hardware.commands import (
    DeviceResponse,
    ExecutionContext,
    ExecutionEvidenceFragment,
    ResourceLedgerDelta,
)
from autonomous_lab.hardware.errors import AmbiguousDeviceStateError, DeviceExecutionError
from autonomous_lab.hardware.simulated._base import quantity_in_range
from autonomous_lab.hardware.simulated.state import SimulatedRunState


def _volume_range() -> QuantityRange:
    return QuantityRange(
        minimum=QuantityValue(value=Decimal("1"), unit="microliter"),
        maximum=QuantityValue(value=Decimal("250"), unit="microliter"),
        resolution=QuantityValue(value=Decimal("0.1"), unit="microliter"),
    )


class SimulatedLiquidHandler:
    """Stateful liquid operations with adapter-level safety bounds."""

    def __init__(
        self,
        state: SimulatedRunState,
        *,
        device_id: str = "simulated-liquid-handler",
    ) -> None:
        self._state = state
        self._device_id = device_id
        volume_range = _volume_range()
        self._manifest = CapabilityManifest(
            device_id=device_id,
            device_type="liquid_handler",
            adapter_name="autolab.simulated.liquid_handler",
            adapter_version="1",
            supported_commands=frozenset(
                {
                    CommandType.DISPENSE,
                    CommandType.ASPIRATE,
                    CommandType.TRANSFER,
                    CommandType.MIX,
                }
            ),
            ranges={
                "dispense_volume": volume_range,
                "aspirate_volume": volume_range,
                "transfer_volume": volume_range,
                "mix_volume": volume_range,
            },
            supported_vessels=frozenset({"microplate_96_well"}),
            channel_count=8,
            safety_limits={
                "maximum_well_volume": QuantityValue(value=Decimal("300"), unit="microliter")
            },
        )

    @property
    def device_id(self) -> str:
        return self._device_id

    async def capability_manifest(self) -> CapabilityManifest:
        return self._manifest

    async def execute(
        self,
        command: ProtocolCommand,
        context: ExecutionContext,
    ) -> DeviceResponse:
        if context.experiment_id != self._state.experiment_id:
            raise DeviceExecutionError("experiment_mismatch")
        resource_deltas: tuple[ResourceLedgerDelta, ...]
        if isinstance(command, DispenseCommand):
            resource_deltas = self._dispense(command)
        elif isinstance(command, AspirateCommand):
            resource_deltas = self._aspirate(command)
        elif isinstance(command, TransferCommand):
            resource_deltas = self._transfer(command)
        elif isinstance(command, MixCommand):
            self._mix(command)
            resource_deltas = ()
        else:
            raise DeviceExecutionError("unsupported_command")
        completed_at = utc_now()
        fragments = (
            ()
            if not resource_deltas
            else (
                ExecutionEvidenceFragment(
                    fragment_id=uuid5(
                        NAMESPACE_URL,
                        f"autolab:evidence-fragment:{context.idempotency_key}:{self.device_id}",
                    ),
                    experiment_id=context.experiment_id,
                    run_id=context.run_id,
                    source_step_id=context.step_id,
                    source_device_id=self.device_id,
                    resource_deltas=resource_deltas,
                    created_at=completed_at,
                    metadata={"command_type": command.command_type.value},
                ),
            )
        )
        return DeviceResponse(
            device_id=self.device_id,
            command_type=command.command_type,
            completed_at=completed_at,
            evidence_fragments=fragments,
            metadata={"attempt": context.attempt},
        )

    def _dispense(self, command: DispenseCommand) -> tuple[ResourceLedgerDelta, ...]:
        amount = quantity_in_range(
            command.volume,
            self._manifest.ranges["dispense_volume"],
            category="dispense_volume_out_of_range",
        )
        current = self._state.volume(command.destination_vessel_id)
        maximum = self._manifest.safety_limits["maximum_well_volume"].to("microliter").value
        if current + amount > maximum:
            raise DeviceExecutionError("vessel_overflow", retry_safe=False)

        # Mutate before raising: an ambiguous transport failure means the physical
        # effect may really have occurred and therefore must never be replayed.
        self._state.dispense(command.destination_vessel_id, command.material_id, amount)
        if (
            self._state.failures.ambiguous_dispense_failure
            and not self._state.ambiguous_dispense_injected
        ):
            self._state.ambiguous_dispense_injected = True
            raise AmbiguousDeviceStateError(
                "ambiguous_dispense", "dispense acknowledgement was lost"
            )
        return (
            self._resource_delta(
                command.destination_vessel_id,
                command.material_id,
                amount,
            ),
        )

    def _aspirate(self, command: AspirateCommand) -> tuple[ResourceLedgerDelta, ...]:
        amount = quantity_in_range(
            command.volume,
            self._manifest.ranges["aspirate_volume"],
            category="aspirate_volume_out_of_range",
        )
        current = self._state.volume(command.source_vessel_id)
        if current < amount:
            raise DeviceExecutionError("insufficient_source_volume")
        removed = self._state.aspirate(command.source_vessel_id, amount)
        return tuple(
            self._resource_delta(
                command.source_vessel_id,
                material_id,
                -volume,
            )
            for material_id, volume in sorted(removed.items())
        )

    def _transfer(self, command: TransferCommand) -> tuple[ResourceLedgerDelta, ...]:
        amount = quantity_in_range(
            command.volume,
            self._manifest.ranges["transfer_volume"],
            category="transfer_volume_out_of_range",
        )
        source = self._state.volume(command.source_vessel_id)
        destination = self._state.volume(command.destination_vessel_id)
        maximum = self._manifest.safety_limits["maximum_well_volume"].to("microliter").value
        if source < amount:
            raise DeviceExecutionError("insufficient_source_volume")
        if destination + amount > maximum:
            raise DeviceExecutionError("vessel_overflow")
        removed = self._state.transfer(
            command.source_vessel_id,
            command.destination_vessel_id,
            amount,
        )
        return tuple(
            delta
            for material_id, volume in sorted(removed.items())
            for delta in (
                self._resource_delta(command.source_vessel_id, material_id, -volume),
                self._resource_delta(command.destination_vessel_id, material_id, volume),
            )
        )

    def _mix(self, command: MixCommand) -> None:
        amount = quantity_in_range(
            command.volume,
            self._manifest.ranges["mix_volume"],
            category="mix_volume_out_of_range",
        )
        if self._state.volume(command.vessel_id) < amount:
            raise DeviceExecutionError("insufficient_mix_volume")
        self._state.mixed_vessels.add(command.vessel_id)

    async def enter_safe_state(self, reason: str) -> None:
        del reason
        self._state.safe_state_devices.add(self.device_id)

    def _resource_delta(
        self,
        vessel_id: str,
        material_id: str,
        volume: Decimal,
    ) -> ResourceLedgerDelta:
        return ResourceLedgerDelta(
            vessel_id=vessel_id,
            material_id=material_id,
            material_kind=self._state.material_kinds.get(material_id, "other"),
            volume_delta=QuantityValue(value=volume, unit="microliter"),
            properties=dict(self._state.material_properties.get(material_id, {})),
        )
