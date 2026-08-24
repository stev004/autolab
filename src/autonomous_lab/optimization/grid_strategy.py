"""Finite deterministic grid-search baseline."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from uuid import UUID

from autonomous_lab.domain.optimization import OptimizationState
from autonomous_lab.domain.quantities import QuantityValue
from autonomous_lab.optimization.interface import (
    OptimizationProblem,
    OptimizationStrategy,
    SearchSpaceExhausted,
)


class GridSearchStrategy(OptimizationStrategy):
    strategy_name = "grid"

    def __init__(
        self,
        *,
        campaign_id: UUID,
        problem: OptimizationProblem,
        seed: int,
        maximum_grid_size: int = 10_000,
        levels: Mapping[str, Sequence[QuantityValue]] | None = None,
        restored_state: OptimizationState | None = None,
    ) -> None:
        if maximum_grid_size < 1:
            raise ValueError("maximum_grid_size must be positive")
        super().__init__(
            campaign_id=campaign_id,
            problem=problem,
            seed=seed,
            restored_state=restored_state,
        )
        self._names = tuple(sorted(self.problem.parameter_space.parameters))
        self._levels = self._build_levels(levels, maximum_grid_size)
        grid_size = 1
        for values in self._levels:
            grid_size *= len(values)
            if grid_size > maximum_grid_size:
                raise ValueError(
                    f"grid has {grid_size} or more combinations; limit is {maximum_grid_size}"
                )
        self.grid_size = grid_size

    def _build_levels(
        self,
        supplied: Mapping[str, Sequence[QuantityValue]] | None,
        maximum_grid_size: int,
    ) -> tuple[tuple[QuantityValue, ...], ...]:
        if supplied is not None and set(supplied) != set(self._names):
            raise ValueError("explicit grid levels must be supplied for every parameter")
        all_levels: list[tuple[QuantityValue, ...]] = []
        for name in self._names:
            definition = self.problem.parameter_space.parameters[name]
            if supplied is not None:
                supplied_values = tuple(supplied[name])
                if not supplied_values:
                    raise ValueError(f"grid parameter {name!r} has no levels")
                if any(not definition.contains(value) for value in supplied_values):
                    raise ValueError(f"grid parameter {name!r} contains an out-of-bounds level")
                all_levels.append(supplied_values)
                continue
            if definition.resolution is None:
                raise ValueError(
                    f"grid parameter {name!r} requires a resolution or explicit levels"
                )
            regular_count = (
                int((definition.maximum - definition.minimum) // definition.resolution) + 1
            )
            reaches_maximum = (
                definition.minimum + (regular_count - 1) * definition.resolution
                == definition.maximum
            )
            level_count = regular_count if reaches_maximum else regular_count + 1
            if level_count > maximum_grid_size:
                raise ValueError(
                    f"grid parameter {name!r} has {level_count} levels; "
                    f"limit is {maximum_grid_size}"
                )
            generated_values: list[QuantityValue] = []
            current = definition.minimum
            while current <= definition.maximum:
                generated_values.append(QuantityValue(value=current, unit=definition.unit))
                current += definition.resolution
            if generated_values[-1].value != definition.maximum:
                generated_values.append(
                    QuantityValue(value=definition.maximum, unit=definition.unit)
                )
            all_levels.append(tuple(generated_values))
        return tuple(all_levels)

    def _ask_parameter_values(self, suggestion_index: int) -> Mapping[str, QuantityValue]:
        if suggestion_index >= self.grid_size:
            raise SearchSpaceExhausted(f"grid exhausted after {self.grid_size} proposals")
        remaining = suggestion_index
        selected_reversed: list[QuantityValue] = []
        for levels in reversed(self._levels):
            remaining, index = divmod(remaining, len(levels))
            selected_reversed.append(levels[index])
        combination = tuple(reversed(selected_reversed))
        return dict(zip(self._names, combination, strict=True))
