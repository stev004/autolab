from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from autonomous_lab.domain.enums import CommandType
from autonomous_lab.domain.protocols import (
    AspirateCommand,
    DispenseCommand,
    IncubateCommand,
    MeasureAbsorbanceCommand,
    MixCommand,
    TransferCommand,
)
from autonomous_lab.domain.quantities import QuantityValue
from autonomous_lab.hardware.commands import ExecutionContext
from autonomous_lab.hardware.errors import DeviceExecutionError
from autonomous_lab.hardware.simulated import SimulatedExperimentPlan, SimulatedHardwareBackend
from autonomous_lab.simulation.assay_model import (
    AssayDesign,
    ExecutedAssayConditions,
    FailureInjection,
)


def _quantity(value: str, unit: str) -> QuantityValue:
    return QuantityValue(value=Decimal(value), unit=unit)


def _plan() -> SimulatedExperimentPlan:
    design = AssayDesign()
    return SimulatedExperimentPlan(
        experiment_id=uuid4(),
        experiment_ordinal=3,
        campaign_seed=41_027,
        conditions=ExecutedAssayConditions(
            reagent_concentration_mg_ml=0.6,
            reagent_volume_microliter=80,
            sample_volume_microliter=40,
            incubation_minutes=12,
            temperature_celsius=29,
        ),
        design=design,
        measurement_vessel_ids=_measurement_vessels(design),
    )


def _context(plan: SimulatedExperimentPlan, step_id: str) -> ExecutionContext:
    return ExecutionContext(
        experiment_id=plan.experiment_id,
        run_id=uuid4(),
        step_id=step_id,
        sequence=0,
        attempt=1,
        idempotency_key=f"test:{step_id}",
        dispatched_at=datetime.now(UTC),
    )


def _measurement_vessels(design: AssayDesign) -> tuple[str, ...]:
    count = (
        design.blank_replicates
        + len(design.standard_levels) * design.standard_replicates
        + design.control_replicates
        + (design.check_standard_replicates if design.check_standard_level is not None else 0)
    )
    return tuple(f"protocol-well-{index:02d}" for index in range(1, count + 1))


async def _prepare_vessel(
    backend: SimulatedHardwareBackend,
    plan: SimulatedExperimentPlan,
    vessel_id: str,
    *,
    reagent_volume: str = "80",
    sample_volume: str = "40",
    incubation_minutes: str = "12",
    temperature_celsius: str = "29",
) -> None:
    context = _context(plan, "prepare")
    await backend.liquid_handler.execute(
        DispenseCommand(
            material_id="reagent",
            destination_vessel_id=vessel_id,
            volume=_quantity(reagent_volume, "microliter"),
        ),
        context,
    )
    await backend.liquid_handler.execute(
        DispenseCommand(
            material_id="blank",
            destination_vessel_id=vessel_id,
            volume=_quantity(sample_volume, "microliter"),
        ),
        context,
    )
    await backend.liquid_handler.execute(
        MixCommand(
            vessel_id=vessel_id,
            cycles=3,
            volume=_quantity(str(Decimal(reagent_volume) + Decimal(sample_volume)), "microliter"),
        ),
        context,
    )
    await backend.incubator.execute(
        IncubateCommand(
            vessel_ids=(vessel_id,),
            duration=_quantity(incubation_minutes, "minute"),
            temperature=_quantity(temperature_celsius, "degree_Celsius"),
        ),
        context,
    )


def test_simulated_backend_advertises_every_supported_range() -> None:
    backend = asyncio.run(SimulatedHardwareBackend.create(_plan()))
    manifests = {item.device_type: item for item in backend.registry.snapshot.manifests}

    assert manifests["liquid_handler"].supported_commands == frozenset(
        {
            CommandType.DISPENSE,
            CommandType.ASPIRATE,
            CommandType.TRANSFER,
            CommandType.MIX,
        }
    )
    assert set(manifests["liquid_handler"].ranges) == {
        "dispense_volume",
        "aspirate_volume",
        "transfer_volume",
        "mix_volume",
    }
    assert set(manifests["incubator"].ranges) == {
        "incubation_duration",
        "incubation_temperature",
    }
    assert set(manifests["spectrophotometer"].ranges) == {"wavelength"}
    assert len(backend.registry.capability_snapshot_hash) == 64


def test_simulated_liquid_handler_supports_declared_operations() -> None:
    plan = _plan()
    backend = asyncio.run(SimulatedHardwareBackend.create(plan))
    context = _context(plan, "liquid")

    async def run_commands() -> None:
        await backend.liquid_handler.execute(
            DispenseCommand(
                material_id="reagent",
                destination_vessel_id="A1",
                volume=_quantity("100", "microliter"),
            ),
            context,
        )
        await backend.liquid_handler.execute(
            MixCommand(vessel_id="A1", cycles=3, volume=_quantity("50", "microliter")),
            context,
        )
        await backend.liquid_handler.execute(
            TransferCommand(
                source_vessel_id="A1",
                destination_vessel_id="A2",
                volume=_quantity("20", "microliter"),
            ),
            context,
        )
        await backend.liquid_handler.execute(
            AspirateCommand(source_vessel_id="A2", volume=_quantity("5", "microliter")),
            context,
        )

    asyncio.run(run_commands())
    assert backend.state.volume("A1") == Decimal("80")
    assert backend.state.volume("A2") == Decimal("15")
    assert "A1" in backend.state.mixed_vessels


def test_adapter_rejects_out_of_range_command_even_without_validator() -> None:
    plan = _plan()
    backend = asyncio.run(SimulatedHardwareBackend.create(plan))
    command = DispenseCommand(
        material_id="reagent",
        destination_vessel_id="A1",
        volume=_quantity("251", "microliter"),
    )
    with pytest.raises(DeviceExecutionError, match="outside device range"):
        asyncio.run(backend.liquid_handler.execute(command, _context(plan, "unsafe")))
    assert backend.state.volume("A1") == 0


def test_fixed_seed_spectrophotometers_produce_identical_readings() -> None:
    plan = _plan()
    first = asyncio.run(SimulatedHardwareBackend.create(plan))
    second = asyncio.run(SimulatedHardwareBackend.create(plan))
    command = MeasureAbsorbanceCommand(
        vessel_ids=(_measurement_vessels(plan.design)[0],),
        wavelength=_quantity("520", "nanometer"),
    )

    asyncio.run(_prepare_vessel(first, plan, command.vessel_ids[0]))
    asyncio.run(_prepare_vessel(second, plan, command.vessel_ids[0]))
    first_response = asyncio.run(
        first.spectrophotometer.execute(command, _context(plan, "measure"))
    )
    second_response = asyncio.run(
        second.spectrophotometer.execute(command, _context(plan, "measure"))
    )

    assert [item.raw_value for item in first_response.measurements] == [
        item.raw_value for item in second_response.measurements
    ]
    assert len(first_response.measurements) == 1
    assert first_response.measurements[0].vessel_id == command.vessel_ids[0]
    assert first.state.world is not second.state.world


def test_spectrophotometer_evidence_uses_executed_state_and_represents_missing_data() -> None:
    original = _plan()
    first_raw_well = "BLK-01"
    plan = replace(
        original,
        failures=FailureInjection(missing_well_id=first_raw_well),
    )
    backend = asyncio.run(SimulatedHardwareBackend.create(plan))
    vessel_id = _measurement_vessels(plan.design)[0]
    asyncio.run(
        _prepare_vessel(
            backend,
            plan,
            vessel_id,
            reagent_volume="70",
            sample_volume="30",
            incubation_minutes="9",
            temperature_celsius="27",
        )
    )
    response = asyncio.run(
        backend.spectrophotometer.execute(
            MeasureAbsorbanceCommand(
                vessel_ids=(vessel_id,),
                wavelength=_quantity("520", "nanometer"),
            ),
            _context(plan, "measure"),
        )
    )

    assert response.measurements == ()
    assert len(response.evidence_fragments) == 1
    fragment = response.evidence_fragments[0]
    assert fragment.resource_deltas == ()
    assert fragment.exposures == ()
    assert len(fragment.acquisitions) == 1
    assert fragment.acquisitions[0].status == "missing"
    assert fragment.acquisitions[0].value is None


@pytest.mark.parametrize(
    "vessel_ids, category",
    [
        (("unknown-vessel",), "unknown_measurement_vessel"),
        (("protocol-well-01", "protocol-well-01"), "duplicate_measurement_vessel"),
    ],
)
def test_spectrophotometer_rejects_unknown_or_duplicate_vessels(
    vessel_ids: tuple[str, ...], category: str
) -> None:
    plan = _plan()
    backend = asyncio.run(SimulatedHardwareBackend.create(plan))
    command = MeasureAbsorbanceCommand(
        vessel_ids=vessel_ids,
        wavelength=_quantity("520", "nanometer"),
    )
    with pytest.raises(DeviceExecutionError) as raised:
        asyncio.run(backend.spectrophotometer.execute(command, _context(plan, "measure")))
    assert raised.value.category == category


def test_all_devices_enter_safe_state() -> None:
    backend = asyncio.run(SimulatedHardwareBackend.create(_plan()))
    assert asyncio.run(backend.registry.enter_safe_state("test")) is True
    assert backend.state.safe_state_devices == {
        "simulated-liquid-handler",
        "simulated-incubator",
        "simulated-spectrophotometer",
    }
