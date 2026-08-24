"""Seeded random-search baseline."""

from __future__ import annotations

from collections.abc import Mapping

from autonomous_lab.domain.quantities import QuantityValue
from autonomous_lab.optimization._sampling import deterministic_rng, sample_parameter
from autonomous_lab.optimization.interface import OptimizationStrategy


class RandomSearchStrategy(OptimizationStrategy):
    strategy_name = "random"

    def _ask_parameter_values(self, suggestion_index: int) -> Mapping[str, QuantityValue]:
        rng = deterministic_rng(self.seed, self.strategy_name, suggestion_index)
        return {
            name: sample_parameter(rng, definition)
            for name, definition in sorted(self.problem.parameter_space.parameters.items())
        }
