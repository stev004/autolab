"""Simulated incubation capability."""

from __future__ import annotations

from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from autonomous_lab.domain.enums import CommandType
from autonomous_lab.domain.identifiers import utc_now
from autonomous_lab.domain.protocols import (
    CapabilityManifest,
    IncubateCommand,
    ProtocolCommand,
    QuantityRange,
)
from autonomous_lab.domain.quantities import QuantityValue
from autonomous_lab.hardware.commands import (
    DeviceResponse,
    EnvironmentalExposure,
    ExecutionContext,
    ExecutionEvidenceFragment,
)
from autonomous_lab.hardware.errors import DeviceExecutionError
from autonomous_lab.hardware.simulated._base import quantity_in_range
from autonomous_lab.hardware.simulated.state import SimulatedRunState


class SimulatedIncubator:
    def __init__(
        self,
        state: SimulatedRunState,
        *,
        device_id: str = "simulated-incubator",
    ) -> None:
        self._state = state
        self._device_id = device_id
        self._manifest = CapabilityManifest(
            device_id=device_id,
            device_type="incubator",
            adapter_name="autolab.simulated.incubator",
            adapter_version="1",
            supported_commands=frozenset({CommandType.INCUBATE}),
            ranges={
                "incubation_duration": QuantityRange(
                    minimum=QuantityValue(value=Decimal("0.1"), unit="minute"),
                    maximum=QuantityValue(value=Decimal("1440"), unit="minute"),
                    resolution=QuantityValue(value=Decimal("0.1"), unit="minute"),
                ),
                "incubation_temperature": QuantityRange(
                    minimum=QuantityValue(value=Decimal("4"), unit="degree_Celsius"),
                    maximum=QuantityValue(value=Decimal("80"), unit="degree_Celsius"),
                    resolution=QuantityValue(value=Decimal("0.1"), unit="delta_degree_Celsius"),
                ),
            },
            supported_vessels=frozenset({"microplate_96_well"}),
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
        if not isinstance(command, IncubateCommand):
            raise DeviceExecutionError("unsupported_command")
        if context.experiment_id != self._state.experiment_id:
            raise DeviceExecutionError("experiment_mismatch")
        duration = quantity_in_range(
            command.duration,
            self._manifest.ranges["incubation_duration"],
            category="incubation_duration_out_of_range",
        )
        temperature = quantity_in_range(
            command.temperature,
            self._manifest.ranges["incubation_temperature"],
            category="incubation_temperature_out_of_range",
        )
        if self._state.failures.failed_incubation and not self._state.incubation_failure_injected:
            self._state.incubation_failure_injected = True
            raise DeviceExecutionError("failed_incubation", retry_safe=False)
        self._state.record_incubation(
            command.vessel_ids,
            duration_minutes=duration,
            temperature_celsius=temperature,
        )
        completed_at = utc_now()
        fragment = ExecutionEvidenceFragment(
            fragment_id=uuid5(
                NAMESPACE_URL,
                f"autolab:evidence-fragment:{context.idempotency_key}:{self.device_id}",
            ),
            experiment_id=context.experiment_id,
            run_id=context.run_id,
            source_step_id=context.step_id,
            source_device_id=self.device_id,
            exposures=(
                EnvironmentalExposure(
                    vessel_ids=command.vessel_ids,
                    duration=QuantityValue(value=duration, unit="minute"),
                    temperature=QuantityValue(value=temperature, unit="degree_Celsius"),
                ),
            ),
            created_at=completed_at,
            metadata={"command_type": command.command_type.value},
        )
        return DeviceResponse(
            device_id=self.device_id,
            command_type=command.command_type,
            completed_at=completed_at,
            evidence_fragments=(fragment,),
            metadata={"attempt": context.attempt},
        )

    async def enter_safe_state(self, reason: str) -> None:
        del reason
        self._state.safe_state_devices.add(self.device_id)
