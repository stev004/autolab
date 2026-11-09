"""Time-series reconciliation edge cases, the controller registry and import boundaries."""

import ast
import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from culture_fakes import START, ScriptedController, ScriptedODSensor, design, proposal

from autonomous_lab.culture import (
    ControllerError,
    CultureControllerRegistry,
    CultureProtocolCompiler,
    SampleReading,
    SteppedClock,
    reconcile_time_series,
)
from autonomous_lab.domain.metric_registry import metric_definition
from autonomous_lab.hardware.sensors import MeasurementRequest


def _readings(
    sensor: ScriptedODSensor, clock: SteppedClock, ids: list[str]
) -> tuple[SampleReading, ...]:
    async def collect() -> tuple[SampleReading, ...]:
        await sensor.open()
        out: list[SampleReading] = []
        for expected_id in ids:
            request = MeasurementRequest(
                experiment_id=uuid4(),
                run_id=uuid4(),
                sample_id="reactor-1",
                measurement_type="optical_density",
                expected_unit="dimensionless",
            )
            out.append(
                SampleReading(
                    expected_measurement_id=expected_id,
                    request=request,
                    measurement=await sensor.read(request),
                )
            )
        return tuple(out)

    return asyncio.run(collect())


def test_duplicate_and_unplanned_readings_are_flagged_not_counted() -> None:
    plan = CultureProtocolCompiler().compile(proposal(), design()).measurement_plan
    clock = SteppedClock(START)
    asyncio.run(clock.sleep_until(plan.points[0].due_at(START)))
    readings = _readings(ScriptedODSensor(clock=clock), clock, ["od-001", "od-001", "od-999"])
    result = reconcile_time_series(plan, hold_started_at=START, readings=readings)
    assert result.entries[0].status == "matched"
    assert result.entries[0].measurement == readings[0].measurement
    assert {entry.status for entry in result.entries[1:]} == {"missing"}
    assert len(result.unexpected_measurement_ids) == 2


def test_registry_creates_adapters_and_checks_what_they_return() -> None:
    registry = CultureControllerRegistry()
    clock = SteppedClock(START)
    registry.register("scripted", lambda controller_id, settings: ScriptedController(clock=clock))
    registry.register("not-a-controller", lambda controller_id, settings: object())
    assert registry.create("scripted", controller_id="ctrl-1").controller_id == "ctrl-1"
    with pytest.raises(ControllerError) as identity:
        registry.create("scripted", controller_id="ctrl-2")
    assert identity.value.code == "controller_identity_mismatch"
    with pytest.raises(ControllerError) as invalid:
        registry.create("not-a-controller", controller_id="ctrl-1")
    assert invalid.value.code == "controller_adapter_invalid"
    with pytest.raises(ControllerError) as missing:
        registry.create("vendor-x", controller_id="ctrl-1")
    assert missing.value.code == "controller_adapter_unavailable"
    with pytest.raises(ValueError, match="already registered"):
        registry.register("scripted", lambda controller_id, settings: object())


def test_culture_metrics_are_registered_for_objectives() -> None:
    assert metric_definition("final_od750").canonical_unit == "dimensionless"
    assert metric_definition("energy_kwh").canonical_unit == "kilowatt_hour"


def test_culture_package_does_not_import_simulation_optimisation_or_campaigns() -> None:
    forbidden_roots = (
        "autonomous_lab.simulation",
        "autonomous_lab.optimization",
        "autonomous_lab.campaigns",
        "autonomous_lab.orchestration",
    )
    offenders: list[tuple[str, str]] = []
    for source in Path("src/autonomous_lab/culture").rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            offenders += [
                (source.name, name)
                for name in names
                if any(name == root or name.startswith(f"{root}.") for root in forbidden_roots)
            ]
    assert offenders == []
