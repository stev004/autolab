"""Small stateful facade used by simulated hardware backends."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from autonomous_lab.provenance.hashing import canonical_sha256
from autonomous_lab.simulation.assay_model import (
    AssayBatch,
    AssayDesign,
    ExecutedAssayConditions,
    FailureInjection,
    NonlinearAssaySimulator,
)


def _empty_completed_batches() -> dict[str, tuple[str, AssayBatch]]:
    return {}


@dataclass(slots=True)
class AssayWorld:
    """Coherent world shared by simulated liquid, incubation and sensor devices.

    The facade deliberately exposes no response-surface coefficients.  Hardware
    adapters derive executed conditions from successful operations and then
    request the raw batch once.
    """

    simulator: NonlinearAssaySimulator = field(default_factory=NonlinearAssaySimulator)
    _completed: dict[str, tuple[str, AssayBatch]] = field(
        default_factory=_empty_completed_batches,
        init=False,
    )

    def run_batch(
        self,
        *,
        experiment_id: str,
        experiment_ordinal: int,
        campaign_seed: int,
        conditions: ExecutedAssayConditions,
        design: AssayDesign,
        failures: FailureInjection | None = None,
    ) -> AssayBatch:
        injection = failures or FailureInjection()
        input_hash = canonical_sha256(
            {
                "experiment_ordinal": experiment_ordinal,
                "campaign_seed": campaign_seed,
                "conditions": asdict(conditions),
                "design": asdict(design),
                "failures": asdict(injection),
                "simulator_config_hash": self.simulator.config.provenance_hash(),
            }
        )
        completed = self._completed.get(experiment_id)
        if completed is not None:
            completed_hash, completed_batch = completed
            if completed_hash != input_hash:
                raise ValueError("experiment ID was replayed with different simulation inputs")
            return completed_batch
        batch = self.simulator.simulate_batch(
            experiment_id=experiment_id,
            experiment_ordinal=experiment_ordinal,
            campaign_seed=campaign_seed,
            conditions=conditions,
            design=design,
            failures=injection,
        )
        self._completed[experiment_id] = (input_hash, batch)
        return batch

    def completed_batch(self, experiment_id: str) -> AssayBatch | None:
        completed = self._completed.get(experiment_id)
        return None if completed is None else completed[1]
