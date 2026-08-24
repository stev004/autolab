"""Stable parameter sampling helpers private to optimisation strategies."""

from __future__ import annotations

import hashlib
import math
from decimal import ROUND_HALF_UP, Decimal

import numpy as np
from numpy.random import Generator

from autonomous_lab.domain.enums import ParameterScale
from autonomous_lab.domain.parameters import ContinuousParameter
from autonomous_lab.domain.quantities import QuantityValue


def deterministic_rng(seed: int, strategy: str, suggestion_index: int) -> Generator:
    if seed < 0 or suggestion_index < 0:
        raise ValueError("seed and suggestion index must be non-negative")
    payload = f"autolab-optimizer-seed-v1:{seed}:{strategy}:{suggestion_index}".encode()
    digest = hashlib.sha256(payload).digest()
    words = [int.from_bytes(digest[index : index + 4], "big") for index in range(0, 32, 4)]
    return np.random.default_rng(np.random.SeedSequence(words))


def quantize_to_parameter(value: Decimal, parameter: ContinuousParameter) -> Decimal:
    clipped = min(parameter.maximum, max(parameter.minimum, value))
    if parameter.resolution is None:
        return clipped
    steps = ((clipped - parameter.minimum) / parameter.resolution).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    return min(parameter.maximum, parameter.minimum + steps * parameter.resolution)


def sample_parameter(rng: Generator, parameter: ContinuousParameter) -> QuantityValue:
    low = float(parameter.minimum)
    high = float(parameter.maximum)
    if parameter.scale is ParameterScale.LOG:
        sampled = math.exp(float(rng.uniform(math.log(low), math.log(high))))
        magnitude = quantize_to_parameter(Decimal(str(sampled)), parameter)
    elif parameter.resolution is not None:
        step_count = int((parameter.maximum - parameter.minimum) // parameter.resolution)
        selected = int(rng.integers(0, step_count + 1))
        magnitude = parameter.minimum + selected * parameter.resolution
    else:
        magnitude = Decimal(str(float(rng.uniform(low, high))))
    return QuantityValue(value=quantize_to_parameter(magnitude, parameter), unit=parameter.unit)
