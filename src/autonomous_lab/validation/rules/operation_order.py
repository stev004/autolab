"""Deterministic per-vessel protocol-order state machine."""

from __future__ import annotations

from dataclasses import dataclass

from autonomous_lab.domain.protocols import (
    AspirateCommand,
    DispenseCommand,
    IncubateCommand,
    MixCommand,
    TransferCommand,
    ValidationIssue,
)
from autonomous_lab.validation.rules.base import ValidationContext, error

RULE = "operation_order"


@dataclass(slots=True)
class _VesselState:
    has_liquid: bool = False
    mixed: bool = False
    incubated: bool = False
    measured: bool = False


def validate(context: ValidationContext) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    states = {vessel.vessel_id: _VesselState() for vessel in context.protocol.vessels}
    for index, step in enumerate(context.protocol.steps):
        command = step.command
        path = f"steps.{index}"
        if isinstance(command, DispenseCommand):
            state = states.get(command.destination_vessel_id)
            if state is None:
                continue
            if state.mixed or state.incubated or state.measured:
                issues.append(_order_error(path, command.destination_vessel_id, "dispense", RULE))
            state.has_liquid = True
        elif isinstance(command, AspirateCommand):
            state = states.get(command.source_vessel_id)
            if state is None:
                continue
            if not state.has_liquid or state.incubated or state.measured:
                issues.append(_order_error(path, command.source_vessel_id, "aspirate", RULE))
        elif isinstance(command, TransferCommand):
            source = states.get(command.source_vessel_id)
            destination = states.get(command.destination_vessel_id)
            if source is not None and (
                not source.has_liquid or source.incubated or source.measured
            ):
                issues.append(_order_error(path, command.source_vessel_id, "transfer", RULE))
            if destination is not None:
                if destination.mixed or destination.incubated or destination.measured:
                    issues.append(
                        _order_error(path, command.destination_vessel_id, "transfer", RULE)
                    )
                destination.has_liquid = True
        elif isinstance(command, MixCommand):
            state = states.get(command.vessel_id)
            if state is None:
                continue
            if not state.has_liquid or state.incubated or state.measured:
                issues.append(_order_error(path, command.vessel_id, "mix", RULE))
            else:
                state.mixed = True
        elif isinstance(command, IncubateCommand):
            for vessel_id in command.vessel_ids:
                state = states.get(vessel_id)
                if state is None:
                    continue
                if not state.has_liquid or not state.mixed or state.measured:
                    issues.append(_order_error(path, vessel_id, "incubate", RULE))
                else:
                    state.incubated = True
        else:
            for vessel_id in command.vessel_ids:
                state = states.get(vessel_id)
                if state is None:
                    continue
                if not state.incubated or state.measured:
                    issues.append(_order_error(path, vessel_id, "measure_absorbance", RULE))
                else:
                    state.measured = True

    for vessel_id, state in states.items():
        if not state.measured:
            issues.append(
                error(
                    "operation.vessel_not_measured",
                    "steps",
                    f"vessel {vessel_id!r} does not reach a completed measurement state",
                    RULE,
                    details={"vessel_id": vessel_id},
                )
            )
    return tuple(issues)


def _order_error(path: str, vessel_id: str, operation: str, rule: str) -> ValidationIssue:
    return error(
        "operation.illegal_order",
        path,
        f"operation {operation!r} is invalid for the current state of vessel {vessel_id!r}",
        rule,
        details={"vessel_id": vessel_id, "operation": operation},
    )
