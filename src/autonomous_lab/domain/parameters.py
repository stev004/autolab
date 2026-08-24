"""Typed scientific parameter-space definitions."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Literal, cast

from pydantic import field_validator, model_validator

from autonomous_lab.domain.base import DomainModel
from autonomous_lab.domain.enums import ParameterScale
from autonomous_lab.domain.quantities import FiniteDecimal, QuantityValue, validate_unit


class ContinuousParameter(DomainModel):
    kind: Literal["continuous"] = "continuous"
    unit: str
    minimum: FiniteDecimal
    maximum: FiniteDecimal
    scale: ParameterScale = ParameterScale.LINEAR
    resolution: FiniteDecimal | None = None

    @field_validator("unit")
    @classmethod
    def _valid_unit(cls, value: str) -> str:
        return validate_unit(value)

    @field_validator("scale", mode="before")
    @classmethod
    def _serialized_scale(cls, value: object) -> object:
        # A serialized enum string is an exact schema value, not permissive type
        # coercion. Numeric strings remain rejected by strict numeric validators.
        if isinstance(value, str):
            return ParameterScale(value)
        return value

    @model_validator(mode="after")
    def _valid_range(self) -> ContinuousParameter:
        if self.minimum >= self.maximum:
            raise ValueError("minimum must be less than maximum")
        if self.scale is ParameterScale.LOG and self.minimum <= 0:
            raise ValueError("log-scaled parameter minimum must be positive")
        if self.resolution is not None:
            if self.resolution <= 0:
                raise ValueError("resolution must be positive")
            if self.resolution > self.maximum - self.minimum:
                raise ValueError("resolution cannot exceed the parameter range")
        return self

    def contains(self, value: QuantityValue) -> bool:
        converted = value.to(self.unit)
        return self.minimum <= converted.value <= self.maximum

    def quantity(self, value: int | float) -> QuantityValue:
        quantity = QuantityValue(value=Decimal(str(value)), unit=self.unit)
        if not self.contains(quantity):
            raise ValueError(
                f"value {value} {self.unit} is outside [{self.minimum}, {self.maximum}]"
            )
        return quantity


class ParameterSpace(DomainModel):
    """Named parameter definitions.

    The model accepts either ``{"parameters": {...}}`` or the concise campaign
    representation where the mapping itself is the parameter space.
    """

    parameters: dict[str, ContinuousParameter]

    @model_validator(mode="before")
    @classmethod
    def _accept_concise_mapping(cls, value: object) -> object:
        original: object = value
        if isinstance(value, Mapping) and "parameters" not in value:
            mapping = cast(Mapping[str, object], value)
            return {"parameters": dict(mapping)}
        return original

    @model_validator(mode="after")
    def _nonempty(self) -> ParameterSpace:
        if not self.parameters:
            raise ValueError("parameter space must not be empty")
        for name in self.parameters:
            if not name.strip():
                raise ValueError("parameter names must not be empty")
        return self

    def validate_values(self, values: Mapping[str, QuantityValue]) -> None:
        expected = set(self.parameters)
        actual = set(values)
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            raise ValueError(f"parameter mismatch; missing={missing}, extra={extra}")
        for name, value in values.items():
            if not self.parameters[name].contains(value):
                definition = self.parameters[name]
                raise ValueError(
                    f"parameter {name!r}={value.value} {value.unit} is outside "
                    f"[{definition.minimum}, {definition.maximum}] {definition.unit}"
                )
