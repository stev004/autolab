"""Core ask/tell contracts.

Strategies propose scientific parameter quantities only.  This module has no
protocol, orchestration, hardware, or simulation imports; the campaign runner owns
all execution and reports outcomes through :meth:`OptimizationStrategy.tell`.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from autonomous_lab.analysis.pareto import ParetoPoint, pareto_front
from autonomous_lab.domain.experiments import ExperimentProposal
from autonomous_lab.domain.objectives import MetricConstraint, ObjectiveMetric
from autonomous_lab.domain.optimization import OptimizationObservation, OptimizationState
from autonomous_lab.domain.parameters import ParameterSpace
from autonomous_lab.domain.quantities import QuantityValue


class ObjectiveDefinition(Protocol):
    metrics: tuple[ObjectiveMetric, ...]
    performance_constraints: tuple[MetricConstraint, ...]


@dataclass(frozen=True, slots=True)
class OptimizationProblem:
    parameter_space: ParameterSpace
    objective_metrics: tuple[ObjectiveMetric, ...]
    performance_constraints: tuple[MetricConstraint, ...]

    def __post_init__(self) -> None:
        unsupported = tuple(
            constraint.operator.value
            for constraint in self.performance_constraints
            if constraint.operator.value not in {"lte", "gte"}
        )
        if unsupported:
            raise ValueError(
                "V0 constrained optimisation supports only lte/gte operators; "
                f"received {unsupported}"
            )

    @classmethod
    def from_models(
        cls, parameter_space: ParameterSpace, objective: ObjectiveDefinition
    ) -> OptimizationProblem:
        return cls(parameter_space, objective.metrics, objective.performance_constraints)

    @property
    def objective_names(self) -> tuple[str, ...]:
        return tuple(metric.name for metric in self.objective_metrics)

    @property
    def constraint_names(self) -> tuple[str, ...]:
        return tuple(constraint.metric for constraint in self.performance_constraints)


class FeedbackDisposition(StrEnum):
    OBSERVED = "observed"
    QC_FAILED = "qc_failed"
    EXECUTION_FAILED = "execution_failed"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class StrategyFeedback:
    strategy_trial_id: str
    disposition: FeedbackDisposition
    observation: OptimizationObservation | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not self.strategy_trial_id:
            raise ValueError("strategy_trial_id must not be empty")
        if self.disposition is FeedbackDisposition.OBSERVED and self.observation is None:
            raise ValueError("observed feedback requires an observation")
        if self.disposition is not FeedbackDisposition.OBSERVED and self.observation is not None:
            raise ValueError("non-observed feedback cannot carry an optimiser observation")

    @classmethod
    def observed(cls, observation: OptimizationObservation, trial_id: str) -> StrategyFeedback:
        return cls(trial_id, FeedbackDisposition.OBSERVED, observation)

    @classmethod
    def excluded(
        cls, trial_id: str, disposition: FeedbackDisposition, *, reason: str
    ) -> StrategyFeedback:
        if disposition is FeedbackDisposition.OBSERVED:
            raise ValueError("use StrategyFeedback.observed for scientific observations")
        return cls(trial_id, disposition, reason=reason)


@dataclass(frozen=True, slots=True)
class TrialRecord:
    proposal: ExperimentProposal
    feedback: StrategyFeedback

    def __post_init__(self) -> None:
        if self.proposal.strategy_trial_id != self.feedback.strategy_trial_id:
            raise ValueError("proposal and feedback trial IDs do not match")


class SearchSpaceExhausted(RuntimeError):
    pass


class UnknownTrialError(KeyError):
    pass


class DuplicateFeedbackError(RuntimeError):
    pass


class OptimizationStrategy(ABC):
    """Stateful one-at-a-time ask/tell strategy with an auditable trial ledger."""

    strategy_name: str
    strategy_version: str = "1.0.0"

    def __init__(
        self,
        *,
        campaign_id: UUID,
        problem: OptimizationProblem,
        seed: int,
        restored_state: OptimizationState | None = None,
    ) -> None:
        if seed < 0:
            raise ValueError("optimisation seed must be non-negative")
        if not problem.objective_metrics:
            raise ValueError("at least one objective metric is required")
        self.campaign_id = campaign_id
        self.problem = problem
        self.seed = seed
        self._proposals: dict[str, ExperimentProposal] = {}
        self._terminal: dict[str, FeedbackDisposition] = {}
        self._observations: dict[str, OptimizationObservation] = {}
        self._suggestion_count = 0
        self._observation_count = 0
        if restored_state is not None:
            self._restore_common_state(restored_state)

    @abstractmethod
    def _ask_parameter_values(self, suggestion_index: int) -> Mapping[str, QuantityValue]:
        """Return parameter values without constructing protocol/device commands."""

    def _trial_id(self, suggestion_index: int) -> str:
        return f"{self.strategy_name}:{suggestion_index}"

    def ask(self) -> ExperimentProposal:
        suggestion_index = self._suggestion_count
        values = dict(self._ask_parameter_values(suggestion_index))
        self.problem.parameter_space.validate_values(values)
        trial_id = self._trial_id(suggestion_index)
        proposal = self._build_proposal(suggestion_index, trial_id, values)
        self._register_proposal(proposal)
        self._suggestion_count += 1
        return proposal

    def tell(self, feedback: StrategyFeedback) -> bool:
        """Record one terminal outcome; return whether it was learned from."""

        self._validate_feedback(feedback)
        learned = self._tell_backend(feedback)
        self._record_feedback(feedback)
        return learned

    def _tell_backend(self, feedback: StrategyFeedback) -> bool:
        return feedback.disposition is FeedbackDisposition.OBSERVED

    def _validate_feedback(self, feedback: StrategyFeedback) -> None:
        if feedback.strategy_trial_id in self._terminal:
            raise DuplicateFeedbackError(
                f"trial already has terminal feedback: {feedback.strategy_trial_id}"
            )
        proposal = self._proposals.get(feedback.strategy_trial_id)
        if proposal is None:
            raise UnknownTrialError(feedback.strategy_trial_id)
        observation = feedback.observation
        if feedback.disposition is FeedbackDisposition.OBSERVED:
            if observation is None:
                raise ValueError("observed feedback requires an observation")
            if not observation.qc_eligible:
                raise ValueError("QC-ineligible results must not be observed by an optimiser")
            if observation.experiment_id != proposal.experiment_id:
                raise ValueError("observation experiment ID does not match its proposal")
            if set(observation.objective_values) != set(self.problem.objective_names):
                raise ValueError(
                    "observation objective values do not match the objective definition"
                )
            if set(observation.constraint_residuals) != set(self.problem.constraint_names):
                raise ValueError(
                    "observation constraint residuals do not match the constraint definition"
                )
            expected_feasible = all(
                value <= 0 for value in observation.constraint_residuals.values()
            )
            if observation.feasible != expected_feasible:
                raise ValueError(
                    "observation feasibility is inconsistent with constraint residuals"
                )

    def _record_feedback(self, feedback: StrategyFeedback) -> None:
        self._terminal[feedback.strategy_trial_id] = feedback.disposition
        if feedback.disposition is FeedbackDisposition.OBSERVED:
            observation = feedback.observation
            if observation is None:
                raise AssertionError("validated observed feedback lost its observation")
            self._observations[feedback.strategy_trial_id] = observation
            self._observation_count += 1

    def reconcile(self, records: Sequence[TrialRecord]) -> None:
        """Rebuild the ledger from authoritative persisted trial records.

        Reconciliation is idempotent.  It never calls ``tell`` twice for a terminal
        backend trial; backend-specific strategies may replay records when needed.
        """

        for record in sorted(records, key=lambda item: item.proposal.suggestion_index):
            trial_id = record.proposal.strategy_trial_id
            existing = self._terminal.get(trial_id)
            if existing is not None:
                if existing is not record.feedback.disposition:
                    raise ValueError(f"conflicting authoritative feedback for trial {trial_id}")
                self._ingest_reconciled_observation(record.feedback)
                continue
            self.problem.parameter_space.validate_values(record.proposal.parameter_values)
            self._register_proposal(record.proposal)
            self._validate_feedback(record.feedback)
            self._reconcile_backend(record)
            self._record_feedback(record.feedback)
            self._suggestion_count = max(
                self._suggestion_count, record.proposal.suggestion_index + 1
            )

    def reconcile_pending(
        self,
        records: Sequence[TrialRecord],
        pending: ExperimentProposal,
    ) -> None:
        """Restore terminal history plus one durable, not-yet-observed proposal."""

        self.reconcile(records)
        self.problem.parameter_space.validate_values(pending.parameter_values)
        if pending.strategy_trial_id in self._terminal:
            raise ValueError("pending proposal already has terminal feedback")
        self._register_proposal(pending)
        self._suggestion_count = max(
            self._suggestion_count,
            pending.suggestion_index + 1,
        )

    def _reconcile_backend(self, record: TrialRecord) -> None:
        del record

    def _ingest_reconciled_observation(self, feedback: StrategyFeedback) -> None:
        if feedback.disposition is not FeedbackDisposition.OBSERVED:
            return
        observation = feedback.observation
        if observation is None:
            raise ValueError("observed authoritative record has no observation")
        existing = self._observations.get(feedback.strategy_trial_id)
        if existing is not None and existing.observation_id != observation.observation_id:
            raise ValueError("conflicting authoritative observation IDs")
        if existing is None:
            self._observations[feedback.strategy_trial_id] = observation

    def pareto_points(self) -> tuple[ParetoPoint, ...]:
        points = (
            ParetoPoint(
                identifier=str(observation.experiment_id),
                objectives=tuple(
                    float(observation.objective_values[name])
                    for name in self.problem.objective_names
                ),
                feasible=observation.feasible,
            )
            for observation in self._observations.values()
        )
        directions = tuple(metric.direction.value for metric in self.problem.objective_metrics)
        return pareto_front(points, directions=directions)

    def checkpoint(self) -> OptimizationState:
        return OptimizationState(
            campaign_id=self.campaign_id,
            strategy_name=self.strategy_name,
            strategy_version=self.strategy_version,
            seed=self.seed,
            suggestion_count=self._suggestion_count,
            observation_count=self._observation_count,
            checkpoint_data={
                "terminal_trials": {
                    trial_id: disposition.value
                    for trial_id, disposition in sorted(self._terminal.items())
                },
                **self._backend_checkpoint_data(),
            },
            updated_at=datetime.now(UTC),
        )

    def _backend_checkpoint_data(self) -> dict[str, object]:
        return {}

    def _restore_common_state(self, state: OptimizationState) -> None:
        if state.campaign_id != self.campaign_id:
            raise ValueError("optimizer state belongs to a different campaign")
        if state.strategy_name != self.strategy_name:
            raise ValueError("optimizer state belongs to a different strategy")
        if state.seed != self.seed:
            raise ValueError("optimizer state seed does not match")
        self._suggestion_count = state.suggestion_count
        self._observation_count = state.observation_count
        raw_terminal = state.checkpoint_data.get("terminal_trials", {})
        if not isinstance(raw_terminal, dict):
            raise ValueError("terminal_trials checkpoint data must be a mapping")
        terminal_items = cast(dict[object, object], raw_terminal)
        self._terminal = {
            str(trial_id): FeedbackDisposition(str(disposition))
            for trial_id, disposition in terminal_items.items()
        }

    def _build_proposal(
        self,
        suggestion_index: int,
        trial_id: str,
        values: Mapping[str, QuantityValue],
    ) -> ExperimentProposal:
        proposal_id = uuid5(
            NAMESPACE_URL,
            f"autolab:{self.campaign_id}:{self.strategy_name}:{suggestion_index}:proposal",
        )
        experiment_id = uuid5(
            NAMESPACE_URL,
            f"autolab:{self.campaign_id}:{self.strategy_name}:{suggestion_index}:experiment",
        )
        return ExperimentProposal(
            proposal_id=proposal_id,
            experiment_id=experiment_id,
            campaign_id=self.campaign_id,
            suggestion_index=suggestion_index,
            parameter_values=dict(values),
            strategy_name=self.strategy_name,
            strategy_version=self.strategy_version,
            strategy_trial_id=trial_id,
        )

    def _register_proposal(self, proposal: ExperimentProposal) -> None:
        existing = self._proposals.get(proposal.strategy_trial_id)
        if existing is not None and existing != proposal:
            raise ValueError(f"conflicting proposal for trial {proposal.strategy_trial_id}")
        self._proposals[proposal.strategy_trial_id] = proposal


def decimal_from_float(value: float) -> Decimal:
    if not math.isfinite(value):
        raise ValueError("optimiser parameter must be finite")
    return Decimal(str(value))
