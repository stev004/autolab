"""Canonical scientific metric names, units, and V0 optimisation roles."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from autonomous_lab.domain.enums import ConstraintOperator, ObjectiveDirection


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    canonical_unit: str
    objective_directions: frozenset[ObjectiveDirection] = frozenset()
    constraint_operators: frozenset[ConstraintOperator] = frozenset()


_MINIMIZE = frozenset({ObjectiveDirection.MINIMIZE})
_MAXIMIZE = frozenset({ObjectiveDirection.MAXIMIZE})
_LTE = frozenset({ConstraintOperator.LTE})
_GTE = frozenset({ConstraintOperator.GTE})

METRIC_REGISTRY: MappingProxyType[str, MetricDefinition] = MappingProxyType(
    {
        "cost_per_test": MetricDefinition("GBP", _MINIMIZE),
        "reagent_consumption": MetricDefinition("milligram", _MINIMIZE),
        "reagent_volume": MetricDefinition("microliter", _MINIMIZE),
        "blank_mean": MetricDefinition("dimensionless", constraint_operators=_LTE),
        "blank_sd": MetricDefinition("dimensionless", constraint_operators=_LTE),
        "calibration_slope": MetricDefinition("dimensionless", _MAXIMIZE, _GTE),
        "calibration_r_squared": MetricDefinition("dimensionless", constraint_operators=_GTE),
        "calibration_slope_standard_error": MetricDefinition(
            "dimensionless", constraint_operators=_LTE
        ),
        "control_mean": MetricDefinition("dimensionless"),
        "control_sd": MetricDefinition("dimensionless", constraint_operators=_LTE),
        "control_cv": MetricDefinition("dimensionless", constraint_operators=_LTE),
        "signal_to_noise": MetricDefinition("dimensionless", _MAXIMIZE, _GTE),
        "sensitivity": MetricDefinition("dimensionless", _MAXIMIZE, _GTE),
        "response_standard_uncertainty": MetricDefinition(
            "dimensionless", constraint_operators=_LTE
        ),
    }
)


def metric_definition(name: str) -> MetricDefinition:
    try:
        return METRIC_REGISTRY[name]
    except KeyError as error:
        raise ValueError(f"unknown scientific metric: {name!r}") from error
