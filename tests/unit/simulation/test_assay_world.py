from dataclasses import replace

import pytest

from autonomous_lab.simulation.assay_model import AssayDesign, ExecutedAssayConditions
from autonomous_lab.simulation.assay_world import AssayWorld


def _conditions() -> ExecutedAssayConditions:
    return ExecutedAssayConditions(
        reagent_concentration_mg_ml=0.6,
        reagent_volume_microliter=80,
        sample_volume_microliter=40,
        incubation_minutes=12,
        temperature_celsius=29,
    )


def test_replay_returns_cached_batch_only_for_identical_complete_inputs() -> None:
    world = AssayWorld()
    inputs = {
        "experiment_id": "experiment-1",
        "experiment_ordinal": 3,
        "campaign_seed": 41_027,
        "conditions": _conditions(),
        "design": AssayDesign(),
    }

    first = world.run_batch(**inputs)
    replay = world.run_batch(**inputs)

    assert replay is first


def test_replay_rejects_same_experiment_id_with_different_scientific_inputs() -> None:
    world = AssayWorld()
    conditions = _conditions()
    inputs = {
        "experiment_id": "experiment-1",
        "experiment_ordinal": 3,
        "campaign_seed": 41_027,
        "conditions": conditions,
        "design": AssayDesign(),
    }
    world.run_batch(**inputs)

    with pytest.raises(ValueError, match="replayed with different simulation inputs"):
        world.run_batch(
            **{
                **inputs,
                "conditions": replace(conditions, reagent_volume_microliter=81),
            }
        )
