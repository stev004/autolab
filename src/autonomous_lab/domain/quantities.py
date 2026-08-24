"""Project-owned quantity abstraction backed by Pint.

Pint objects are intentionally not exposed in persisted schemas. ``QuantityValue``
is the stable wire representation; conversion and dimensional checks go through
the functions in this module.
"""

from __future__ import annotations

from contextlib import suppress
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Annotated, Any, Protocol, cast

import pint
from pydantic import BeforeValidator, field_validator

from autonomous_lab.domain.base import DomainModel


def _finite_decimal(value: object) -> Decimal:
    if isinstance(value, (bool, str)):
        raise ValueError("expected a finite JSON number, not a string or boolean")
    if not isinstance(value, (Decimal, int, float)):
        raise ValueError("expected a finite numeric value")
    try:
        converted = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("expected a finite numeric value") from exc
    if not converted.is_finite():
        raise ValueError("numeric value must be finite")
    return converted


FiniteDecimal = Annotated[Decimal, BeforeValidator(_finite_decimal)]


class _PintQuantity(Protocol):
    """Narrow typed surface used to isolate incomplete Pint type information."""

    magnitude: Decimal
    units: object
    dimensionality: object

    def to(self, other: str) -> _PintQuantity: ...

    def to_base_units(self) -> _PintQuantity: ...


@lru_cache(maxsize=1)
def unit_registry() -> pint.UnitRegistry[Decimal]:
    """Return AutoLab's sole configured unit registry."""

    # Decimal conversion factors avoid binary-float artifacts (including offset
    # temperature conversions) in persisted scientific values.
    registry: pint.UnitRegistry[Decimal] = pint.UnitRegistry(
        autoconvert_offset_to_baseunit=True,
        non_int_type=cast(Any, Decimal),
    )
    with suppress(pint.errors.RedefinitionError):
        registry.define("GBP = [currency]")
    return registry


def validate_unit(unit: str) -> str:
    """Validate a unit expression and return its original stable spelling."""

    if not unit.strip():
        raise ValueError("unit must not be empty")
    try:
        unit_registry().Unit(unit)
    except (pint.errors.UndefinedUnitError, ValueError) as exc:
        raise ValueError(f"unknown or invalid unit: {unit!r}") from exc
    return unit


class QuantityValue(DomainModel):
    """Serializable magnitude and unit with dimensional conversion helpers."""

    value: FiniteDecimal
    unit: str

    @field_validator("unit")
    @classmethod
    def _valid_unit(cls, value: str) -> str:
        return validate_unit(value)

    def to(self, unit: str) -> QuantityValue:
        """Convert to a compatible unit or raise ``ValueError``."""

        validate_unit(unit)
        registry = unit_registry()
        try:
            source = cast(_PintQuantity, registry.Quantity(self.value, self.unit))
            converted = source.to(unit)
        except (pint.errors.DimensionalityError, pint.errors.OffsetUnitCalculusError) as exc:
            raise ValueError(f"{self.unit!r} is not compatible with {unit!r}") from exc
        return QuantityValue(value=Decimal(str(converted.magnitude)), unit=unit)

    def is_compatible_with(self, other: QuantityValue | str) -> bool:
        """Return whether this quantity has the same dimensionality as ``other``."""

        other_unit = other.unit if isinstance(other, QuantityValue) else other
        try:
            self.to(other_unit)
        except ValueError:
            return False
        return True

    @property
    def dimensionality(self) -> str:
        """Return Pint's stable dimensionality expression."""

        quantity = cast(_PintQuantity, unit_registry().Quantity(self.value, self.unit))
        return str(quantity.dimensionality)

    def canonical(self) -> QuantityValue:
        """Convert common laboratory quantities to project canonical units."""

        registry = unit_registry()
        quantity = cast(_PintQuantity, registry.Quantity(self.value, self.unit))
        canonical_candidates = (
            "microliter",
            "milligram / milliliter",
            "minute",
            "degree_Celsius",
            "nanometer",
            "GBP",
            "dimensionless",
        )
        for candidate in canonical_candidates:
            try:
                converted = quantity.to(candidate)
            except (pint.errors.DimensionalityError, pint.errors.OffsetUnitCalculusError):
                continue
            return QuantityValue(value=Decimal(str(converted.magnitude)), unit=candidate)
        base = quantity.to_base_units()
        return QuantityValue(value=Decimal(str(base.magnitude)), unit=str(base.units))

    def __lt__(self, other: QuantityValue) -> bool:
        converted = other.to(self.unit)
        return self.value < converted.value

    def __le__(self, other: QuantityValue) -> bool:
        converted = other.to(self.unit)
        return self.value <= converted.value


def require_compatible(quantity: QuantityValue, expected_unit: str) -> QuantityValue:
    """Return ``quantity`` converted to ``expected_unit``, raising on mismatch."""

    return quantity.to(expected_unit)


def quantity_from(value: Any, unit: str) -> QuantityValue:
    """Construct a quantity through the same strict numeric validation as schemas."""

    return QuantityValue(value=value, unit=unit)
