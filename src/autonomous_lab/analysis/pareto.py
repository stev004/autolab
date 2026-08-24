"""Small, deterministic multi-objective Pareto helpers."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ParetoPoint:
    identifier: str
    objectives: tuple[float, ...]
    feasible: bool = True

    def __post_init__(self) -> None:
        if not self.objectives:
            raise ValueError("at least one objective is required")
        if any(not math.isfinite(value) for value in self.objectives):
            raise ValueError("objectives must be finite")


def dominates(
    left: ParetoPoint,
    right: ParetoPoint,
    *,
    directions: Sequence[str] | None = None,
    tolerance: float = 0.0,
) -> bool:
    if len(left.objectives) != len(right.objectives):
        raise ValueError("objective dimensions must agree")
    selected = directions or ("minimize",) * len(left.objectives)
    if len(selected) != len(left.objectives):
        raise ValueError("one direction is required per objective")
    no_worse = True
    strictly_better = False
    for left_value, right_value, direction in zip(
        left.objectives, right.objectives, selected, strict=True
    ):
        if direction == "minimize":
            no_worse &= left_value <= right_value + tolerance
            strictly_better |= left_value < right_value - tolerance
        elif direction == "maximize":
            no_worse &= left_value >= right_value - tolerance
            strictly_better |= left_value > right_value + tolerance
        else:
            raise ValueError(f"unsupported direction: {direction}")
    return no_worse and strictly_better


def pareto_front(
    points: Iterable[ParetoPoint],
    *,
    directions: Sequence[str] | None = None,
    feasible_only: bool = True,
    tolerance: float = 0.0,
) -> tuple[ParetoPoint, ...]:
    candidates = tuple(point for point in points if point.feasible or not feasible_only)
    if not candidates:
        return ()
    dimensions = len(candidates[0].objectives)
    if any(len(point.objectives) != dimensions for point in candidates):
        raise ValueError("all objective vectors must have equal dimensions")
    front = [
        point
        for index, point in enumerate(candidates)
        if not any(
            dominates(other, point, directions=directions, tolerance=tolerance)
            for other_index, other in enumerate(candidates)
            if other_index != index
        )
    ]
    return tuple(sorted(front, key=lambda point: (point.objectives, point.identifier)))


def hypervolume_2d(points: Iterable[ParetoPoint], reference_point: tuple[float, float]) -> float:
    """Dominated hypervolume for feasible, two-objective minimisation points."""

    if len(reference_point) != 2 or any(not math.isfinite(value) for value in reference_point):
        raise ValueError("reference point must contain two finite values")
    front = pareto_front(points)
    eligible = sorted(
        (
            point
            for point in front
            if len(point.objectives) == 2
            and point.objectives[0] <= reference_point[0]
            and point.objectives[1] <= reference_point[1]
        ),
        key=lambda point: point.objectives[0],
    )
    if any(len(point.objectives) != 2 for point in front):
        raise ValueError("hypervolume_2d requires two objectives")
    area = 0.0
    previous_y = reference_point[1]
    for point in eligible:
        x, y = point.objectives
        if y < previous_y:
            area += (reference_point[0] - x) * (previous_y - y)
            previous_y = y
    return area
