"""Simulated absorbance device backed by the hidden assay world."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from autonomous_lab.domain.enums import CommandType
from autonomous_lab.domain.identifiers import utc_now
from autonomous_lab.domain.protocols import (
    CapabilityManifest,
    MeasureAbsorbanceCommand,
    ProtocolCommand,
    QuantityRange,
)
from autonomous_lab.domain.quantities import QuantityValue
from autonomous_lab.hardware.commands import (
    AcquisitionRecord,
    DeviceResponse,
    ExecutionContext,
    ExecutionEvidenceFragment,
)
from autonomous_lab.hardware.errors import DeviceExecutionError, DeviceTimeoutError
from autonomous_lab.hardware.simulated._base import quantity_in_range
from autonomous_lab.hardware.simulated.state import SimulatedRunState
from autonomous_lab.simulation.assay_model import (
    AssayDesign,
    RawAssayMeasurement,
    WellRole,
)
from autonomous_lab.simulation.domain_adapters import (
    measurements_from_simulated_batch,
    simulated_measurement_identifier,
)


class SimulatedSpectrophotometer:
    def __init__(
        self,
        state: SimulatedRunState,
        *,
        design: AssayDesign,
        experiment_ordinal: int,
        campaign_seed: int,
        measurement_vessel_ids: tuple[str, ...] | None = None,
        device_id: str = "simulated-spectrophotometer",
    ) -> None:
        self._state = state
        self._design = design
        self._experiment_ordinal = experiment_ordinal
        self._campaign_seed = campaign_seed
        self._measurement_vessel_ids = measurement_vessel_ids
        self._device_id = device_id
        self._manifest = CapabilityManifest(
            device_id=device_id,
            device_type="spectrophotometer",
            adapter_name="autolab.simulated.spectrophotometer",
            adapter_version="1",
            supported_commands=frozenset({CommandType.MEASURE_ABSORBANCE}),
            ranges={
                "wavelength": QuantityRange(
                    minimum=QuantityValue(value=Decimal("400"), unit="nanometer"),
                    maximum=QuantityValue(value=Decimal("700"), unit="nanometer"),
                    resolution=QuantityValue(value=Decimal("1"), unit="nanometer"),
                )
            },
            supported_vessels=frozenset({"microplate_96_well"}),
            channel_count=96,
            calibration_id="simulated-calibration-v1",
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
        if not isinstance(command, MeasureAbsorbanceCommand):
            raise DeviceExecutionError("unsupported_command")
        if context.experiment_id != self._state.experiment_id:
            raise DeviceExecutionError("experiment_mismatch")
        if len(command.vessel_ids) != len(set(command.vessel_ids)):
            raise DeviceExecutionError("duplicate_measurement_vessel")
        if self._measurement_vessel_ids is not None:
            unknown = set(command.vessel_ids) - set(self._measurement_vessel_ids)
            if unknown:
                raise DeviceExecutionError(
                    "unknown_measurement_vessel",
                    f"requested vessels are absent from the configured layout: {sorted(unknown)!r}",
                )
        wavelength = quantity_in_range(
            command.wavelength,
            self._manifest.ranges["wavelength"],
            category="wavelength_out_of_range",
        )
        expected = Decimal(str(self._design.wavelength_nanometer))
        if wavelength != expected:
            raise DeviceExecutionError("measurement_plan_mismatch")
        if (
            self._state.failures.spectrophotometer_timeout
            and not self._state.spectrophotometer_timeout_injected
        ):
            self._state.spectrophotometer_timeout_injected = True
            raise DeviceTimeoutError("simulated spectrophotometer timed out")

        simulator_failures = replace(
            self._state.failures,
            spectrophotometer_timeout=False,
            failed_incubation=False,
            ambiguous_dispense_failure=False,
        )
        try:
            executed_conditions = self._state.executed_conditions(command.vessel_ids)
        except ValueError as exc:
            raise DeviceExecutionError("executed_condition_mismatch", str(exc)) from exc
        batch = self._state.world.run_batch(
            experiment_id=str(context.experiment_id),
            experiment_ordinal=self._experiment_ordinal,
            campaign_seed=self._campaign_seed,
            conditions=executed_conditions,
            design=self._design,
            failures=simulator_failures,
        )
        rebound = self._bind_protocol_vessels(batch.measurements, command.vessel_ids)
        requested = set(command.vessel_ids)
        batch = replace(
            batch,
            measurements=tuple(item for item in rebound if item.well_id in requested),
        )
        measured_at = utc_now()
        present_batch = replace(
            batch,
            measurements=tuple(item for item in batch.measurements if item.absorbance is not None),
        )
        measurements = measurements_from_simulated_batch(
            present_batch,
            experiment_id=UUID(str(context.experiment_id)),
            run_id=context.run_id,
            measured_at=measured_at,
            step_id=context.step_id,
            device_id=self.device_id,
        )
        acquisitions = tuple(
            AcquisitionRecord(
                acquisition_id=simulated_measurement_identifier(
                    item,
                    run_id=context.run_id,
                    step_id=context.step_id,
                    device_id=self.device_id,
                ),
                experiment_id=context.experiment_id,
                run_id=context.run_id,
                step_id=context.step_id,
                device_id=self.device_id,
                vessel_id=item.well_id,
                measurement_name="absorbance",
                status="missing" if item.absorbance is None else "ok",
                value=(
                    None
                    if item.absorbance is None
                    else QuantityValue(
                        value=Decimal(str(item.absorbance)),
                        unit="dimensionless",
                    )
                ),
                acquired_at=measured_at,
                metadata={
                    "role": item.role.value,
                    "analyte_level": item.analyte_concentration,
                    "replicate_index": item.replicate,
                    "acquisition_index": item.acquisition_index,
                    "wavelength_nanometer": item.wavelength_nanometer,
                    "random_stream_id": item.stream_id,
                    "raw_simulator_measurement_id": item.measurement_id,
                },
            )
            for item in batch.measurements
        )
        fragment = ExecutionEvidenceFragment(
            fragment_id=uuid5(
                NAMESPACE_URL,
                f"autolab:evidence-fragment:{context.idempotency_key}:{self.device_id}",
            ),
            experiment_id=context.experiment_id,
            run_id=context.run_id,
            source_step_id=context.step_id,
            source_device_id=self.device_id,
            acquisitions=acquisitions,
            created_at=measured_at,
            metadata={
                "command_type": command.command_type.value,
                "simulator_model_id": batch.model_id,
                "simulator_model_version": batch.model_version,
                "simulator_config_hash": batch.simulator_config_hash,
            },
        )
        return DeviceResponse(
            device_id=self.device_id,
            command_type=command.command_type,
            completed_at=measured_at,
            measurements=measurements,
            evidence_fragments=(fragment,),
            metadata={"attempt": context.attempt},
        )

    async def enter_safe_state(self, reason: str) -> None:
        del reason
        self._state.safe_state_devices.add(self.device_id)

    def _bind_protocol_vessels(
        self,
        measurements: tuple[RawAssayMeasurement, ...],
        protocol_vessel_ids: tuple[str, ...],
    ) -> tuple[RawAssayMeasurement, ...]:
        """Replace simulator aliases with exact compiled protocol vessel IDs."""

        expected_keys: list[tuple[WellRole, float, int]] = []
        expected_keys.extend(
            (WellRole.BLANK, 0.0, replicate)
            for replicate in range(1, self._design.blank_replicates + 1)
        )
        for level in self._design.standard_levels:
            expected_keys.extend(
                (WellRole.STANDARD, level, replicate)
                for replicate in range(1, self._design.standard_replicates + 1)
            )
        expected_keys.extend(
            (WellRole.CONTROL, self._design.control_level, replicate)
            for replicate in range(1, self._design.control_replicates + 1)
        )
        if self._design.check_standard_level is not None:
            check_replicates = self._design.check_standard_replicates
            expected_keys.extend(
                (WellRole.CHECK_STANDARD, self._design.check_standard_level, replicate)
                for replicate in range(1, check_replicates + 1)
            )
        raw_by_key = {
            (item.role, item.analyte_concentration, item.replicate): item.well_id
            for item in measurements
        }
        layout_vessel_ids = self._measurement_vessel_ids
        if layout_vessel_ids is None:
            if len(protocol_vessel_ids) == len(expected_keys):
                layout_vessel_ids = protocol_vessel_ids
            else:
                layout_vessel_ids = tuple(raw_by_key[key] for key in expected_keys)
        if len(layout_vessel_ids) != len(expected_keys):
            raise DeviceExecutionError(
                "measurement_vessel_count_mismatch",
                f"configured layout has {len(layout_vessel_ids)} vessels; "
                f"assay design requires {len(expected_keys)}",
            )
        if len(protocol_vessel_ids) != len(set(protocol_vessel_ids)):
            raise DeviceExecutionError("duplicate_measurement_vessel")
        unknown = set(protocol_vessel_ids) - set(layout_vessel_ids)
        if unknown:
            raise DeviceExecutionError(
                "unknown_measurement_vessel",
                f"requested vessels are absent from the configured layout: {sorted(unknown)!r}",
            )
        target_by_key = dict(zip(expected_keys, layout_vessel_ids, strict=True))
        rebound: list[RawAssayMeasurement] = []
        for item in measurements:
            key = (item.role, item.analyte_concentration, item.replicate)
            target = target_by_key.get(key)
            if target is None:
                raise DeviceExecutionError(
                    "measurement_layout_mismatch",
                    f"simulator reading {key!r} is absent from the compiled layout",
                )
            rebound.append(
                replace(
                    item,
                    well_id=target,
                    measurement_id=f"{item.experiment_id}:{target}:absorbance",
                )
            )
        return tuple(rebound)
