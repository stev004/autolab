"""Liquid-volume bounds and vessel-capacity accounting."""

from __future__ import annotations

from decimal import Decimal

from autonomous_lab.domain.protocols import (
    AspirateCommand,
    DispenseCommand,
    MixCommand,
    TransferCommand,
    ValidationIssue,
)
from autonomous_lab.domain.quantities import QuantityValue
from autonomous_lab.validation.rules.base import ValidationContext, error

RULE = "liquid_volumes"


def validate(context: ValidationContext) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    capacities: dict[str, Decimal] = {}
    volumes: dict[str, Decimal] = {
        vessel.vessel_id: Decimal("0") for vessel in context.protocol.vessels
    }
    policy_capacity = context.policy.maximum_well_volume.to("microliter").value
    for index, vessel in enumerate(context.protocol.vessels):
        try:
            capacity = vessel.maximum_volume.to("microliter").value
        except ValueError:
            issues.append(
                error(
                    "vessel.capacity_unit_incompatible",
                    f"vessels.{index}.maximum_volume",
                    "vessel capacity must be a volume",
                    RULE,
                )
            )
            continue
        capacities[vessel.vessel_id] = capacity
        if capacity <= 0:
            issues.append(
                error(
                    "vessel.nonsensical_capacity",
                    f"vessels.{index}.maximum_volume",
                    "vessel capacity must be positive",
                    RULE,
                )
            )
        if capacity > policy_capacity:
            issues.append(
                error(
                    "vessel.capacity_exceeds_policy",
                    f"vessels.{index}.maximum_volume",
                    "declared vessel capacity exceeds trusted policy",
                    RULE,
                    details={
                        "capacity_ul": str(capacity),
                        "policy_maximum_ul": str(policy_capacity),
                    },
                )
            )

    for index, step in enumerate(context.protocol.steps):
        command = step.command
        path = f"steps.{index}.command.volume"
        if isinstance(command, (DispenseCommand, AspirateCommand, TransferCommand)):
            volume = _liquid_operation_volume(context, issues, command.volume, path)
            if volume is None:
                continue
            if isinstance(command, DispenseCommand):
                _add_volume(
                    issues,
                    volumes,
                    capacities,
                    command.destination_vessel_id,
                    volume,
                    path,
                )
            elif isinstance(command, AspirateCommand):
                _remove_volume(
                    issues,
                    volumes,
                    command.source_vessel_id,
                    volume,
                    path,
                )
            else:
                removed = _remove_volume(
                    issues,
                    volumes,
                    command.source_vessel_id,
                    volume,
                    path,
                )
                if removed:
                    _add_volume(
                        issues,
                        volumes,
                        capacities,
                        command.destination_vessel_id,
                        volume,
                        path,
                    )
        elif isinstance(command, MixCommand):
            try:
                mix_volume = command.volume.to("microliter").value
            except ValueError:
                issues.append(
                    error(
                        "volume.unit_incompatible",
                        path,
                        "mix volume must be a volume",
                        RULE,
                    )
                )
                continue
            if mix_volume <= 0:
                issues.append(
                    error(
                        "volume.nonsensical_nonpositive",
                        path,
                        "mix volume must be positive",
                        RULE,
                    )
                )
            available = volumes.get(command.vessel_id)
            if available is not None and mix_volume > available:
                issues.append(
                    error(
                        "volume.mix_exceeds_contents",
                        path,
                        "mix volume exceeds the liquid currently in the vessel",
                        RULE,
                        details={
                            "mix_volume_ul": str(mix_volume),
                            "available_volume_ul": str(available),
                        },
                    )
                )
    return tuple(issues)


def _liquid_operation_volume(
    context: ValidationContext,
    issues: list[ValidationIssue],
    quantity: QuantityValue,
    path: str,
) -> Decimal | None:
    try:
        value = quantity.to("microliter").value
    except ValueError:
        issues.append(
            error(
                "volume.unit_incompatible",
                path,
                "liquid-operation volume must have volume units",
                RULE,
            )
        )
        return None
    minimum = context.policy.minimum_dispense_volume.to("microliter").value
    maximum = context.policy.maximum_dispense_volume.to("microliter").value
    if value <= 0:
        issues.append(
            error(
                "volume.nonsensical_nonpositive",
                path,
                "liquid-operation volume must be positive",
                RULE,
            )
        )
    if not minimum <= value <= maximum:
        issues.append(
            error(
                "volume.outside_policy",
                path,
                "liquid-operation volume is outside trusted policy limits",
                RULE,
                details={
                    "volume_ul": str(value),
                    "minimum_ul": str(minimum),
                    "maximum_ul": str(maximum),
                },
            )
        )
    return value


def _add_volume(
    issues: list[ValidationIssue],
    volumes: dict[str, Decimal],
    capacities: dict[str, Decimal],
    vessel_id: str,
    value: Decimal,
    path: str,
) -> None:
    if vessel_id not in volumes:
        return
    updated = volumes[vessel_id] + value
    volumes[vessel_id] = updated
    capacity = capacities.get(vessel_id)
    if capacity is not None and updated > capacity:
        issues.append(
            error(
                "volume.vessel_capacity_exceeded",
                path,
                f"operation exceeds capacity of vessel {vessel_id!r}",
                RULE,
                details={"resulting_volume_ul": str(updated), "capacity_ul": str(capacity)},
            )
        )


def _remove_volume(
    issues: list[ValidationIssue],
    volumes: dict[str, Decimal],
    vessel_id: str,
    value: Decimal,
    path: str,
) -> bool:
    if vessel_id not in volumes:
        return False
    if value > volumes[vessel_id]:
        issues.append(
            error(
                "volume.insufficient_source",
                path,
                f"operation removes more liquid than vessel {vessel_id!r} contains",
                RULE,
                details={
                    "requested_volume_ul": str(value),
                    "available_volume_ul": str(volumes[vessel_id]),
                },
            )
        )
        return False
    volumes[vessel_id] -= value
    return True
