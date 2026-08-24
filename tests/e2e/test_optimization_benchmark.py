from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from sqlalchemy import select

from autonomous_lab.analysis.pareto import ParetoPoint, hypervolume_2d
from autonomous_lab.campaigns.loader import load_campaign, load_validation_policy
from autonomous_lab.campaigns.service import CampaignService
from autonomous_lab.domain.enums import CampaignState
from autonomous_lab.storage import Database, Storage, UnitOfWork
from autonomous_lab.storage.orm import ExperimentRecord, OptimizerObservationRecord

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CAMPAIGN_PATH = PROJECT_ROOT / "configs" / "assay-reagent-reduction.yaml"
POLICY_PATH = PROJECT_ROOT / "configs" / "validation-policy.yaml"
# A conservative dominated reference derived only from declared parameter maxima
# and the public cost model (not from the hidden assay response surface).
REFERENCE_POINT = (0.12, 0.22)
REFERENCE_AREA = REFERENCE_POINT[0] * REFERENCE_POINT[1]


def test_seeded_bayesian_campaign_improves_normalized_pareto_hypervolume(
    tmp_path: Path,
) -> None:
    """The public loop must improve without access to the simulator response surface."""

    loaded = load_campaign(CAMPAIGN_PATH)
    config = loaded.config.model_copy(
        update={
            "campaign": loaded.config.campaign.model_copy(
                update={"max_experiments": 60, "seed": 20260814}
            ),
            "optimization": loaded.config.optimization.model_copy(
                update={"strategy": "bayesian", "startup_experiments": 12}
            ),
            "simulation": loaded.config.simulation.model_copy(
                update={"outlier_probability": Decimal("0")}
            ),
        }
    )
    storage = Storage(Database(tmp_path / "benchmark.sqlite3"))
    storage.initialize()
    service = CampaignService(
        storage,
        validation_policy=load_validation_policy(POLICY_PATH),
        optimizer_root=tmp_path / "optimizer",
        prefer_gp=False,
    )
    campaign_id = service.create(
        replace(loaded, config=config),
        campaign_id=UUID("a47b5e30-a690-54f1-8869-b2ce79636525"),
    )

    status = asyncio.run(service.run(campaign_id))
    points = _observation_points(storage, campaign_id)
    initial_points = [point for point in points if int(point.identifier) < 12]
    initial_hypervolume = hypervolume_2d(initial_points, REFERENCE_POINT) / REFERENCE_AREA
    final_hypervolume = hypervolume_2d(points, REFERENCE_POINT) / REFERENCE_AREA

    assert status.state is CampaignState.COMPLETED
    assert status.experiment_count == 60
    assert any(point.feasible for point in points)
    assert final_hypervolume > initial_hypervolume, (
        initial_hypervolume,
        final_hypervolume,
    )


@pytest.mark.slow
def test_bayesian_and_random_outcome_distributions_across_fixed_seeds(
    tmp_path: Path,
) -> None:
    """A slower aggregate guard against mistaking one fortunate run for learning."""

    seeds = (1103, 2207, 3301)
    scores = {
        strategy: tuple(
            _final_hypervolume(
                tmp_path / f"{strategy}-{seed}",
                strategy=strategy,
                seed=seed,
                budget=40,
            )
            for seed in seeds
        )
        for strategy in ("random", "bayesian")
    }
    random_mean = sum(scores["random"]) / len(seeds)
    bayesian_mean = sum(scores["bayesian"]) / len(seeds)

    assert all(score > 0 for distribution in scores.values() for score in distribution)
    # On this small, noisy V0 surface, constrained TPE is not guaranteed to beat
    # random search in every finite run. Guard against a pathological regression
    # while retaining the paired distributions for honest benchmark reporting.
    assert bayesian_mean >= 0.9 * random_mean, scores
    assert any(
        bayesian > random
        for bayesian, random in zip(scores["bayesian"], scores["random"], strict=True)
    ), scores


def _observation_points(storage: Storage, campaign_id: UUID) -> list[ParetoPoint]:
    with UnitOfWork(storage.database) as uow:
        assert uow.session is not None
        rows = uow.session.execute(
            select(
                ExperimentRecord.ordinal,
                OptimizerObservationRecord.objective_values,
                OptimizerObservationRecord.feasible,
            )
            .join(
                OptimizerObservationRecord,
                OptimizerObservationRecord.experiment_id == ExperimentRecord.id,
            )
            .where(ExperimentRecord.campaign_id == str(campaign_id))
            .order_by(ExperimentRecord.ordinal)
        ).all()
    return [
        ParetoPoint(
            identifier=str(ordinal),
            objectives=(
                float(objectives["cost_per_test"]),
                float(objectives["reagent_consumption"]),
            ),
            feasible=feasible,
        )
        for ordinal, objectives, feasible in rows
    ]


def _final_hypervolume(
    root: Path,
    *,
    strategy: str,
    seed: int,
    budget: int,
) -> float:
    root.mkdir()
    loaded = load_campaign(CAMPAIGN_PATH)
    config = loaded.config.model_copy(
        update={
            "campaign": loaded.config.campaign.model_copy(
                update={"max_experiments": budget, "seed": seed}
            ),
            "optimization": loaded.config.optimization.model_copy(
                update={"strategy": strategy, "startup_experiments": min(12, budget)}
            ),
            "simulation": loaded.config.simulation.model_copy(
                update={"outlier_probability": Decimal("0")}
            ),
        }
    )
    storage = Storage(Database(root / "benchmark.sqlite3"))
    storage.initialize()
    service = CampaignService(
        storage,
        validation_policy=load_validation_policy(POLICY_PATH),
        optimizer_root=root / "optimizer",
        prefer_gp=False,
    )
    campaign_id = uuid5(NAMESPACE_URL, f"autolab-benchmark:{strategy}:{seed}:{budget}")
    service.create(replace(loaded, config=config), campaign_id=campaign_id)
    status = asyncio.run(service.run(campaign_id))
    points = _observation_points(storage, campaign_id)

    assert status.state is CampaignState.COMPLETED
    assert any(point.feasible for point in points)
    return hypervolume_2d(points, REFERENCE_POINT) / REFERENCE_AREA
