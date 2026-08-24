"""Shared helpers for simulated adapter bounds and responses."""

from __future__ import annotations

from decimal import Decimal

from autonomous_lab.domain.protocols import QuantityRange
from autonomous_lab.domain.quantities import QuantityValue
from autonomous_lab.hardware.errors import DeviceExecutionError


def quantity_in_range(
    value: QuantityValue,
    allowed: QuantityRange,
    *,
    category: str,
) -> Decimal:
    converted = value.to(allowed.minimum.unit)
    maximum = allowed.maximum.to(allowed.minimum.unit)
    if converted.value < allowed.minimum.value or converted.value > maximum.value:
        raise DeviceExecutionError(
            category,
            f"{converted.value} {converted.unit} is outside device range "
            f"[{allowed.minimum.value}, {maximum.value}]",
        )
    return converted.value
