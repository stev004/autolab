from __future__ import annotations

import numpy as np
import pytest

from autonomous_lab.simulation.random_streams import independent_rng, stable_seed_words


def test_stable_seed_derivation_is_repeatable_and_identifier_sensitive() -> None:
    first = stable_seed_words(41, "campaign", "experiment-1", "noise")
    second = stable_seed_words(41, "campaign", "experiment-1", "noise")
    other = stable_seed_words(41, "campaign", "experiment-1", "drift")

    assert first == second
    assert first != other
    assert len(first) == 8


def test_independent_streams_do_not_depend_on_access_order() -> None:
    noise_before = independent_rng(9, "experiment", "noise").normal(size=8)
    independent_rng(9, "experiment", "new-component").normal(size=100)
    noise_after = independent_rng(9, "experiment", "noise").normal(size=8)

    np.testing.assert_array_equal(noise_before, noise_after)


def test_negative_seed_is_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        independent_rng(-1, "component")
