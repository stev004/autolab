"""Device capability, range, resolution, and calibration checks."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from autonomous_lab.domain.protocols import (
    AspirateCommand,
    DispenseCommand,
    IncubateCommand,
    MixCommand,
    ProtocolCommand,
    QuantityRange,
    TransferCommand,
    ValidationIssue,
)
from autonomous_lab.domain.quantities import QuantityValue
from autonomous_lab.validation.rules.base import ValidationContext, error

RULE = "hardware_capabilities"


def validate(context: ValidationContext) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    manifests = context.capability_snapshot.manifests
    device_ids = [manifest.device_id for manifest in manifests]
    if len(device_ids) != len(set(device_ids)):
        issues.append(
            error(
                "capability.duplicate_device_id",
                "capability_snapshot.manifests",
                "capability snapshot device IDs must be unique",
                RULE,
            )
        )
    vessel_types = {
        vessel.vessel_id: vessel.vessel_type for vessel in context.protocol.vessels
    }
    for index, step in enumerate(context.protocol.steps):
        command = step.command
        candidates = [
            manifest
            for manifest in manifests
            if command.command_type in manifest.supported_commands
            and step.required_capability
            in {
                manifest.device_id,
                manifest.device_type,
                command.command_type.value,
            }
        ]
        compatible: list[str] = []
        rejection_reasons: dict[str, list[str]] = {}
        for manifest in candidates:
            reasons = _manifest_rejection_reasons(
                command,
                manifest.ranges,
                vessel_types,
                manifest.supported_vessels,
            )
            if manifest.calibration_expires_at is not None and (
                manifest.calibration_expires_at <= context.validation_time
            ):
                reasons.append("calibration_expired")
            if reasons:
                rejection_reasons[manifest.device_id] = reasons
            else:
                compatible.append(manifest.device_id)
        if not compatible:
            code = "capability.unsupported_command" if not candidates else "capability.out_of_range"
            issues.append(
                error(
                    code,
                    f"steps.{index}",
                    "no device capability can safely execute this protocol step",
                    RULE,
                    details={
                        "command_type": command.command_type.value,
                        "required_capability": step.required_capability,
                        "candidate_rejections": rejection_reasons,
                    },
                )
            )
    return tuple(issues)


def validate_policy_measurement(context: ValidationContext) -> tuple[ValidationIssue, ...]:
    wavelength = context.protocol.measurement_plan.wavelength
    reasons = _range_reasons(wavelength, context.policy.wavelength_range)
    if not reasons:
        return ()
    return (
        error(
            "measurement.wavelength_outside_policy",
            "measurement_plan.wavelength",
            "measurement wavelength violates trusted policy range or resolution",
            RULE,
            details={"reasons": reasons},
        ),
    )


def _manifest_rejection_reasons(
    command: ProtocolCommand,
    ranges: dict[str, QuantityRange],
    vessel_types: dict[str, str],
    supported_vessels: frozenset[str],
) -> list[str]:
    reasons: list[str] = []
    command_vessels = _command_vessel_ids(command)
    required_types = {
        vessel_types[vessel_id] for vessel_id in command_vessels if vessel_id in vessel_types
    }
    if not required_types.issubset(supported_vessels):
        reasons.append("unsupported_vessel_type")
    for range_name, quantity in _required_ranges(command):
        supported_range = ranges.get(range_name)
        if supported_range is None:
            reasons.append(f"missing_range:{range_name}")
            continue
        reasons.extend(
            f"{range_name}:{reason}" for reason in _range_reasons(quantity, supported_range)
        )
    return reasons


def _command_vessel_ids(command: ProtocolCommand) -> tuple[str, ...]:
    if isinstance(command, DispenseCommand):
        return (command.destination_vessel_id,)
    if isinstance(command, AspirateCommand):
        return (command.source_vessel_id,)
    if isinstance(command, TransferCommand):
        return (command.source_vessel_id, command.destination_vessel_id)
    if isinstance(command, MixCommand):
        return (command.vessel_id,)
    return command.vessel_ids


def _required_ranges(command: ProtocolCommand) -> tuple[tuple[str, QuantityValue], ...]:
    if isinstance(command, DispenseCommand):
        return (("dispense_volume", command.volume),)
    if isinstance(command, AspirateCommand):
        return (("aspirate_volume", command.volume),)
    if isinstance(command, TransferCommand):
        return (("transfer_volume", command.volume),)
    if isinstance(command, MixCommand):
        return (("mix_volume", command.volume),)
    if isinstance(command, IncubateCommand):
        return (
            ("incubation_duration", command.duration),
            ("incubation_temperature", command.temperature),
        )
    return (("wavelength", command.wavelength),)


def _range_reasons(quantity: QuantityValue, supported: QuantityRange) -> list[str]:
    try:
        value = quantity.to(supported.minimum.unit).value
        maximum = supported.maximum.to(supported.minimum.unit).value
    except ValueError:
        return ["incompatible_unit"]
    if not supported.minimum.value <= value <= maximum:
        return ["outside_bounds"]
    if supported.resolution is None:
        return []
    resolution_value = _resolution_magnitude(supported.resolution, supported.minimum.unit)
    if resolution_value is None:
        return ["invalid_resolution"]
    try:
        step_count = (value - supported.minimum.value) / resolution_value
    except (InvalidOperation, ZeroDivisionError):
        return ["invalid_resolution"]
    if step_count != step_count.to_integral_value():
        return ["unsupported_resolution"]
    return []


def _resolution_magnitude(resolution: QuantityValue, range_unit: str) -> Decimal | None:
    try:
        return resolution.to(range_unit).value
    except ValueError:
        # Offset temperature ranges intentionally use delta units. Their numeric
        # interval is directly comparable when dimensionality has already been
        # validated by QuantityRange.
        if resolution.dimensionality == QuantityValue(
            value=Decimal("0"), unit=range_unit
        ).dimensionality:
            return resolution.value
        return None
