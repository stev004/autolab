from __future__ import annotations

import pytest

from autonomous_lab.analysis.pareto import ParetoPoint, hypervolume_2d, pareto_front


def test_pareto_front_retains_only_feasible_nondominated_points() -> None:
    points = (
        ParetoPoint("a", (1.0, 5.0)),
        ParetoPoint("b", (2.0, 3.0)),
        ParetoPoint("c", (3.0, 4.0)),
        ParetoPoint("d", (0.5, 2.0), feasible=False),
    )

    assert [point.identifier for point in pareto_front(points)] == ["a", "b"]


def test_two_dimensional_hypervolume_is_union_not_double_counted() -> None:
    points = (
        ParetoPoint("a", (1.0, 5.0)),
        ParetoPoint("b", (2.0, 3.0)),
    )

    # Rectangle a: 4*1; additional rectangle b below a: 3*2.
    assert hypervolume_2d(points, (5.0, 6.0)) == pytest.approx(10.0)


def test_maximisation_direction_is_supported() -> None:
    points = (
        ParetoPoint("weak", (1.0, 5.0)),
        ParetoPoint("strong", (2.0, 4.0)),
    )

    assert pareto_front(points, directions=("maximize", "minimize")) == (points[1],)
