"""Optuna ask/tell adapter for constrained multi-objective optimisation."""

from __future__ import annotations

import importlib
import math
import warnings
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import cast
from uuid import UUID

import optuna
from optuna.distributions import BaseDistribution, FloatDistribution
from optuna.samplers import BaseSampler, GPSampler, TPESampler
from optuna.study import Study
from optuna.trial import FrozenTrial, Trial, TrialState, create_trial

from autonomous_lab.domain.enums import ParameterScale
from autonomous_lab.domain.experiments import ExperimentProposal
from autonomous_lab.domain.optimization import OptimizationState
from autonomous_lab.domain.quantities import QuantityValue
from autonomous_lab.optimization._sampling import quantize_to_parameter
from autonomous_lab.optimization.interface import (
    FeedbackDisposition,
    OptimizationProblem,
    OptimizationStrategy,
    StrategyFeedback,
    TrialRecord,
)

_CONSTRAINTS_ATTRIBUTE = "autolab_constraint_residuals"
_PARAMETERS_ATTRIBUTE = "autolab_executed_parameter_values"


class OptunaReconciliationError(ValueError):
    """The native Optuna study conflicts with the authoritative trial ledger."""


def _constraints_from_trial(trial: FrozenTrial) -> Sequence[float]:
    raw = trial.user_attrs.get(_CONSTRAINTS_ATTRIBUTE, ())
    if not isinstance(raw, (list, tuple)):
        return ()
    values: list[float] = []
    for value in cast(Sequence[object], raw):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return ()
        values.append(float(value))
    return tuple(values)


class OptunaStrategy(OptimizationStrategy):
    """Persistent external-loop Bayesian optimiser.

    GP sampling is selected only when PyTorch is importable.  The fallback is
    constrained multivariate TPE, which remains multi-objective and ask/tell based.
    """

    strategy_name = "bayesian"
    strategy_version = "1.1.0"

    def __init__(
        self,
        *,
        campaign_id: UUID,
        problem: OptimizationProblem,
        seed: int,
        startup_experiments: int = 12,
        storage_url: str | None = None,
        study_name: str | None = None,
        prefer_gp: bool = True,
        restored_state: OptimizationState | None = None,
    ) -> None:
        if startup_experiments < 0:
            raise ValueError("startup_experiments must be non-negative")
        unsupported = tuple(
            name
            for name, parameter in sorted(problem.parameter_space.parameters.items())
            if parameter.scale is ParameterScale.LOG and parameter.resolution is not None
        )
        if unsupported:
            raise ValueError(
                "Optuna does not support quantized log distributions; "
                f"parameters={unsupported}"
            )
        self.startup_experiments = startup_experiments
        self.storage_url = storage_url
        self.study_name = study_name or f"autolab-{campaign_id}"
        self._pending_trials: dict[str, Trial] = {}
        self._trial_number_by_index: dict[int, int] = {}
        self._orphaned_native_trials: set[int] = set()
        self._locally_sampled_trials: set[int] = set()
        self._sampler_reconstructed = False
        sampler, sampler_name = self._select_sampler(
            self._sampler_seed(seed, 0), startup_experiments, prefer_gp
        )
        self.sampler_name = sampler_name
        directions = [metric.direction.value for metric in problem.objective_metrics]
        self.study: Study = optuna.create_study(
            study_name=self.study_name,
            storage=storage_url,
            sampler=sampler,
            directions=directions,
            load_if_exists=True,
        )
        super().__init__(
            campaign_id=campaign_id,
            problem=problem,
            seed=seed,
            restored_state=restored_state,
        )
        existing_count = len(self.study.trials)
        if existing_count > self._suggestion_count:
            self._suggestion_count = existing_count
        self._sampler_replay_required = self._suggestion_count > 0
        if restored_state is not None:
            expected_sampler = restored_state.checkpoint_data.get("sampler_name")
            if expected_sampler is not None and str(expected_sampler) != self.sampler_name:
                # A missing optional GP dependency may legitimately cause fallback,
                # but silently changing samplers on resume would harm reproducibility.
                raise ValueError(
                    f"restored sampler {expected_sampler!r} is unavailable; "
                    f"selected {self.sampler_name!r}"
                )

    @staticmethod
    def _select_sampler(
        seed: int, startup_experiments: int, prefer_gp: bool
    ) -> tuple[BaseSampler, str]:
        torch_available = False
        if prefer_gp:
            try:
                importlib.import_module("torch")
                torch_available = True
            except (ImportError, OSError):
                torch_available = False
        if prefer_gp and torch_available:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", optuna.exceptions.ExperimentalWarning)
                    return (
                        GPSampler(
                            seed=seed,
                            n_startup_trials=startup_experiments,
                            deterministic_objective=False,
                            constraints_func=_constraints_from_trial,
                        ),
                        "optuna_gp",
                    )
            except ImportError:
                pass
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", optuna.exceptions.ExperimentalWarning)
            sampler = TPESampler(
                seed=seed,
                n_startup_trials=startup_experiments,
                multivariate=True,
                constraints_func=_constraints_from_trial,
            )
        return sampler, "optuna_tpe"

    def _ask_parameter_values(self, suggestion_index: int) -> Mapping[str, QuantityValue]:
        if self._sampler_replay_required:
            raise RuntimeError(
                "reconcile authoritative records before asking a resumed Optuna study"
            )
        self._install_sampler_for_trial(suggestion_index)
        trial = self.study.ask()
        if trial.number != suggestion_index:
            raise RuntimeError(
                f"Optuna trial number {trial.number} does not match "
                f"suggestion index {suggestion_index}; "
                "reconcile authoritative records before requesting another proposal"
            )
        values = self._suggest_trial_values(trial)
        self._locally_sampled_trials.add(trial.number)
        self._trial_number_by_index[suggestion_index] = trial.number
        self._pending_trials[f"{self.strategy_name}:{trial.number}"] = trial
        trial.set_user_attr(
            _PARAMETERS_ATTRIBUTE,
            {name: str(value.value) for name, value in sorted(values.items())},
        )
        return values

    def _suggest_trial_values(self, trial: Trial) -> dict[str, QuantityValue]:
        values: dict[str, QuantityValue] = {}
        for name, parameter in sorted(self.problem.parameter_space.parameters.items()):
            if parameter.scale is ParameterScale.LOG and parameter.resolution is not None:
                raise ValueError(
                    f"Optuna does not support a quantized log distribution for parameter {name!r}"
                )
            sampled = trial.suggest_float(
                name,
                float(parameter.minimum),
                float(parameter.maximum),
                log=parameter.scale is ParameterScale.LOG,
                step=(float(parameter.resolution) if parameter.resolution is not None else None),
            )
            magnitude = quantize_to_parameter(Decimal(str(sampled)), parameter)
            values[name] = QuantityValue(value=magnitude, unit=parameter.unit)
        return values

    def _trial_id(self, suggestion_index: int) -> str:
        number = self._trial_number_by_index.get(suggestion_index)
        if number is None:
            raise RuntimeError("Optuna did not register a trial for the suggestion")
        return f"{self.strategy_name}:{number}"

    def _tell_backend(self, feedback: StrategyFeedback) -> bool:
        trial = self._pending_trials.pop(feedback.strategy_trial_id, None)
        if trial is None:
            number = self._parse_trial_number(feedback.strategy_trial_id)
            frozen = {item.number: item for item in self.study.trials}.get(number)
            if frozen is None or frozen.state is not TrialState.RUNNING:
                raise RuntimeError(
                    f"Optuna trial {feedback.strategy_trial_id} is not pending; "
                    "reconcile before telling"
                )
            if feedback.disposition is FeedbackDisposition.OBSERVED:
                observation = feedback.observation
                if observation is None:
                    raise AssertionError("validated observed feedback has no observation")
                residuals = [
                    float(observation.constraint_residuals[name])
                    for name in self.problem.constraint_names
                ]
                self._set_frozen_trial_user_attr(
                    frozen, _CONSTRAINTS_ATTRIBUTE, residuals
                )
                self.study.tell(
                    number,
                    values=[
                        float(observation.objective_values[name])
                        for name in self.problem.objective_names
                    ],
                    state=TrialState.COMPLETE,
                )
                return True
            self.study.tell(number, state=TrialState.FAIL)
            return False
        if feedback.disposition is FeedbackDisposition.OBSERVED:
            observation = feedback.observation
            if observation is None:
                raise AssertionError("validated observed feedback has no observation")
            residuals = [
                float(observation.constraint_residuals[name])
                for name in self.problem.constraint_names
            ]
            trial.set_user_attr(_CONSTRAINTS_ATTRIBUTE, residuals)
            values = [
                float(observation.objective_values[name]) for name in self.problem.objective_names
            ]
            self.study.tell(trial, values=values, state=TrialState.COMPLETE)
            return True
        self.study.tell(trial, state=TrialState.FAIL)
        return False

    def reconcile(self, records: Sequence[TrialRecord]) -> None:
        """Reconcile both the framework ledger and Optuna's native study.

        The database trial records are authoritative. Native trials are audited in
        full before the common restored checkpoint can make reconciliation a no-op.
        This catches silent corruption of parameters, distributions, observations,
        or constraint metadata as well as incomplete crash remnants.
        """

        ordered = tuple(sorted(records, key=lambda item: item.proposal.suggestion_index))
        self._reconstruct_sampler_state(ordered)
        authoritative: dict[int, TrialRecord] = {}
        for record in ordered:
            number = self._parse_trial_number(record.proposal.strategy_trial_id)
            if number != record.proposal.suggestion_index:
                raise OptunaReconciliationError(
                    f"Optuna trial {number} does not match authoritative suggestion index "
                    f"{record.proposal.suggestion_index}"
                )
            if number in authoritative:
                raise OptunaReconciliationError(
                    f"authoritative Optuna history contains duplicate trial {number}"
                )
            authoritative[number] = record
            self._reconcile_backend(record)

        self._reconcile_orphans(set(authoritative))
        super().reconcile(ordered)
        self._sampler_replay_required = False

    def reconcile_pending(
        self,
        records: Sequence[TrialRecord],
        pending: ExperimentProposal,
    ) -> None:
        """Audit and restore one native RUNNING trial across a human wait."""

        ordered = tuple(sorted(records, key=lambda item: item.proposal.suggestion_index))
        self._reconstruct_sampler_state(ordered, pending=pending)
        OptimizationStrategy.reconcile(self, ordered)
        number = self._parse_trial_number(pending.strategy_trial_id)
        if number != pending.suggestion_index or number != len(ordered):
            raise OptunaReconciliationError(
                "pending Optuna proposal is not contiguous after terminal history"
            )
        frozen = {item.number: item for item in self.study.trials}.get(number)
        if frozen is None or frozen.state is not TrialState.RUNNING:
            raise OptunaReconciliationError(
                "pending authoritative proposal has no matching native RUNNING trial"
            )
        placeholder = TrialRecord(
            pending,
            StrategyFeedback.excluded(
                pending.strategy_trial_id,
                FeedbackDisposition.EXECUTION_FAILED,
                reason="pending-reconciliation-placeholder",
            ),
        )
        self._audit_trial_shape(frozen, placeholder)
        self._reconcile_orphans(set(range(len(ordered))) | {number})
        self.problem.parameter_space.validate_values(pending.parameter_values)
        if pending.strategy_trial_id in self._terminal:
            raise OptunaReconciliationError("pending proposal already has terminal feedback")
        self._register_proposal(pending)
        self._suggestion_count = max(self._suggestion_count, pending.suggestion_index + 1)
        self._sampler_replay_required = False

    def _reconstruct_sampler_state(
        self,
        records: Sequence[TrialRecord],
        *,
        pending: ExperimentProposal | None = None,
    ) -> None:
        """Rebuild historical proposals with their deterministic per-trial samplers.

        Optuna's RDB storage intentionally does not persist sampler RNG state. AutoLab
        therefore derives an independent seed from the campaign seed and suggestion
        index. Shadow studies validate every historical proposal without modifying the
        execution study, so version drift or an incompatible sampler fails closed.
        """

        if self._sampler_reconstructed:
            return
        if self._locally_sampled_trials:
            self._sampler_reconstructed = True
            return
        if not records and pending is None:
            if self._sampler_replay_required:
                raise OptunaReconciliationError(
                    "cannot reconstruct a resumed Optuna sampler without authoritative trials"
                )
            self._sampler_reconstructed = True
            return

        directions = [metric.direction.value for metric in self.problem.objective_metrics]
        shadow_trials: list[FrozenTrial] = []
        for expected_number, record in enumerate(records):
            number = self._parse_trial_number(record.proposal.strategy_trial_id)
            if number != expected_number or record.proposal.suggestion_index != expected_number:
                raise OptunaReconciliationError(
                    "authoritative Optuna history is not contiguous from trial zero"
                )
            sampler = self._sampler_for_trial(expected_number)
            shadow = optuna.create_study(sampler=sampler, directions=directions)
            for historical_trial in shadow_trials:
                shadow.add_trial(historical_trial)
            shadow_trial = shadow.ask()
            replayed_values = self._suggest_trial_values(shadow_trial)
            self._audit_replayed_proposal(replayed_values, record)
            shadow_trial.set_user_attr(
                _PARAMETERS_ATTRIBUTE,
                {name: str(value.value) for name, value in sorted(replayed_values.items())},
            )
            feedback = record.feedback
            if feedback.disposition is FeedbackDisposition.OBSERVED:
                observation = feedback.observation
                if observation is None:
                    raise AssertionError("authoritative observed record has no observation")
                shadow_trial.set_user_attr(
                    _CONSTRAINTS_ATTRIBUTE,
                    [
                        float(observation.constraint_residuals[name])
                        for name in self.problem.constraint_names
                    ],
                )
                shadow.tell(
                    shadow_trial,
                    values=[
                        float(observation.objective_values[name])
                        for name in self.problem.objective_names
                    ],
                    state=TrialState.COMPLETE,
                )
            else:
                shadow.tell(shadow_trial, state=TrialState.FAIL)
            shadow_trials = shadow.trials
        if pending is not None:
            expected_number = len(records)
            number = self._parse_trial_number(pending.strategy_trial_id)
            if number != expected_number or pending.suggestion_index != expected_number:
                raise OptunaReconciliationError(
                    "pending authoritative Optuna proposal is not contiguous"
                )
            sampler = self._sampler_for_trial(expected_number)
            shadow = optuna.create_study(sampler=sampler, directions=directions)
            for historical_trial in shadow_trials:
                shadow.add_trial(historical_trial)
            shadow_trial = shadow.ask()
            replayed_values = self._suggest_trial_values(shadow_trial)
            placeholder = TrialRecord(
                pending,
                StrategyFeedback.excluded(
                    pending.strategy_trial_id,
                    FeedbackDisposition.EXECUTION_FAILED,
                    reason="pending-replay-placeholder",
                ),
            )
            self._audit_replayed_proposal(replayed_values, placeholder)
        self._sampler_reconstructed = True

    def _install_sampler_for_trial(self, suggestion_index: int) -> None:
        sampler = self._sampler_for_trial(suggestion_index)
        if self.storage_url is not None:
            self.study = optuna.load_study(
                study_name=self.study_name,
                storage=self.storage_url,
                sampler=sampler,
            )
            return

        if self._pending_trials:
            # ``sampler`` is a public Study attribute. Keeping this Study preserves
            # live Trial handles when callers pipeline more than one ask.
            self.study.sampler = sampler
            return
        directions = [metric.direction.value for metric in self.problem.objective_metrics]
        replacement = optuna.create_study(sampler=sampler, directions=directions)
        for frozen in self.study.trials:
            replacement.add_trial(frozen)
        self.study = replacement

    def _sampler_for_trial(self, suggestion_index: int) -> BaseSampler:
        sampler, sampler_name = self._select_sampler(
            self._sampler_seed(self.seed, suggestion_index),
            self.startup_experiments,
            self.sampler_name == "optuna_gp",
        )
        if sampler_name != self.sampler_name:
            raise OptunaReconciliationError(
                f"sampler availability changed from {self.sampler_name!r} to {sampler_name!r}"
            )
        return sampler

    @staticmethod
    def _sampler_seed(seed: int, suggestion_index: int) -> int:
        return (seed + suggestion_index) % (2**32)

    def _audit_replayed_proposal(
        self, replayed: Mapping[str, QuantityValue], record: TrialRecord
    ) -> None:
        expected = record.proposal.parameter_values
        if set(replayed) != set(expected):
            raise OptunaReconciliationError(
                "replayed Optuna parameter names conflict with authoritative proposal"
            )
        for name, value in replayed.items():
            parameter = self.problem.parameter_space.parameters[name]
            actual_magnitude = value.to(parameter.unit).value
            expected_magnitude = expected[name].to(parameter.unit).value
            if actual_magnitude != expected_magnitude:
                raise OptunaReconciliationError(
                    f"replayed Optuna trial {record.proposal.suggestion_index} parameter "
                    f"{name!r} conflicts with authoritative proposal"
                )

    def _reconcile_backend(self, record: TrialRecord) -> None:
        number = self._parse_trial_number(record.proposal.strategy_trial_id)
        existing = {trial.number: trial for trial in self.study.trials}
        frozen = existing.get(number)
        if frozen is not None:
            self._audit_trial_shape(frozen, record)
            expected_complete = record.feedback.disposition is FeedbackDisposition.OBSERVED
            if frozen.state is TrialState.RUNNING:
                if expected_complete:
                    observation = record.feedback.observation
                    if observation is None:
                        raise AssertionError("authoritative observed record has no observation")
                    residuals = [
                        float(observation.constraint_residuals[name])
                        for name in self.problem.constraint_names
                    ]
                    self._set_frozen_trial_user_attr(
                        frozen, _CONSTRAINTS_ATTRIBUTE, residuals
                    )
                    values = [
                        float(observation.objective_values[name])
                        for name in self.problem.objective_names
                    ]
                    self.study.tell(number, values=values, state=TrialState.COMPLETE)
                else:
                    self.study.tell(number, state=TrialState.FAIL)
                self._pending_trials.pop(record.proposal.strategy_trial_id, None)
                repaired = {trial.number: trial for trial in self.study.trials}[number]
                self._audit_trial_shape(repaired, record)
                self._audit_terminal_trial(repaired, record)
                return
            if expected_complete and frozen.state is not TrialState.COMPLETE:
                raise OptunaReconciliationError(
                    f"Optuna trial {number} state conflicts with authoritative observation"
                )
            if not expected_complete and frozen.state is not TrialState.FAIL:
                raise OptunaReconciliationError(
                    f"Optuna trial {number} state conflicts with authoritative failure"
                )
            self._audit_terminal_trial(frozen, record)
            return
        if number != len(existing):
            raise OptunaReconciliationError("authoritative Optuna trial history contains a gap")

        params = self._expected_parameters(record)
        distributions = self._expected_distributions()
        executed_parameters = self._expected_executed_parameters(record)
        if record.feedback.disposition is FeedbackDisposition.OBSERVED:
            observation = record.feedback.observation
            if observation is None:
                raise AssertionError("authoritative observed record has no observation")
            residuals = [
                float(observation.constraint_residuals[name])
                for name in self.problem.constraint_names
            ]
            frozen = create_trial(
                state=TrialState.COMPLETE,
                params=params,
                distributions=distributions,
                values=[
                    float(observation.objective_values[name])
                    for name in self.problem.objective_names
                ],
                user_attrs={
                    _CONSTRAINTS_ATTRIBUTE: residuals,
                    _PARAMETERS_ATTRIBUTE: executed_parameters,
                },
            )
        else:
            frozen = create_trial(
                state=TrialState.FAIL,
                params=params,
                distributions=distributions,
                user_attrs={_PARAMETERS_ATTRIBUTE: executed_parameters},
            )
        self.study.add_trial(frozen)
        replayed = {trial.number: trial for trial in self.study.trials}[number]
        self._audit_trial_shape(replayed, record)
        self._audit_terminal_trial(replayed, record)

    def _audit_trial_shape(self, frozen: FrozenTrial, record: TrialRecord) -> None:
        number = frozen.number
        expected_params = self._expected_parameters(record)
        if set(frozen.params) != set(expected_params):
            raise OptunaReconciliationError(
                f"Optuna trial {number} parameter names conflict with authoritative proposal"
            )
        for name, expected in expected_params.items():
            actual = frozen.params[name]
            if isinstance(actual, bool) or not isinstance(actual, (int, float)):
                raise OptunaReconciliationError(
                    f"Optuna trial {number} parameter {name!r} is not numeric"
                )
            if not math.isclose(float(actual), expected, rel_tol=1e-12, abs_tol=1e-12):
                raise OptunaReconciliationError(
                    f"Optuna trial {number} parameter {name!r} conflicts with "
                    "authoritative proposal"
                )

        expected_distributions = self._expected_distributions()
        if frozen.distributions != expected_distributions:
            raise OptunaReconciliationError(
                f"Optuna trial {number} distributions conflict with authoritative search space"
            )

        raw_executed = frozen.user_attrs.get(_PARAMETERS_ATTRIBUTE)
        if not isinstance(raw_executed, Mapping):
            raise OptunaReconciliationError(
                f"Optuna trial {number} is missing authoritative executed-parameter metadata"
            )
        unknown_mapping = cast(Mapping[object, object], raw_executed)
        if not all(isinstance(key, str) for key in unknown_mapping):
            raise OptunaReconciliationError(
                f"Optuna trial {number} has invalid executed-parameter metadata keys"
            )
        executed_mapping = cast(Mapping[str, object], unknown_mapping)
        if set(executed_mapping) != set(expected_params):
            raise OptunaReconciliationError(
                f"Optuna trial {number} is missing authoritative executed-parameter metadata"
            )
        expected_executed = self._expected_executed_parameters(record)
        for name, expected in expected_executed.items():
            try:
                actual = Decimal(str(executed_mapping[name]))
            except Exception as error:
                raise OptunaReconciliationError(
                    f"Optuna trial {number} executed parameter {name!r} is invalid"
                ) from error
            if actual != Decimal(expected):
                raise OptunaReconciliationError(
                    f"Optuna trial {number} executed parameter {name!r} conflicts with "
                    "authoritative proposal"
                )

    def _audit_terminal_trial(self, frozen: FrozenTrial, record: TrialRecord) -> None:
        number = frozen.number
        feedback = record.feedback
        if feedback.disposition is not FeedbackDisposition.OBSERVED:
            if frozen.values is not None:
                raise OptunaReconciliationError(
                    f"failed Optuna trial {number} unexpectedly contains objective values"
                )
            if _CONSTRAINTS_ATTRIBUTE in frozen.user_attrs:
                raise OptunaReconciliationError(
                    f"failed Optuna trial {number} unexpectedly contains constraint metadata"
                )
            return

        observation = feedback.observation
        if observation is None:
            raise AssertionError("authoritative observed record has no observation")
        expected_values = tuple(
            float(observation.objective_values[name]) for name in self.problem.objective_names
        )
        actual_values = frozen.values
        if actual_values is None or not self._float_sequences_equal(actual_values, expected_values):
            raise OptunaReconciliationError(
                f"Optuna trial {number} objective values conflict with authoritative observation"
            )
        expected_residuals = tuple(
            float(observation.constraint_residuals[name])
            for name in self.problem.constraint_names
        )
        raw_residuals = frozen.user_attrs.get(_CONSTRAINTS_ATTRIBUTE)
        if not isinstance(raw_residuals, (list, tuple)) or not self._float_sequences_equal(
            cast(Sequence[object], raw_residuals), expected_residuals
        ):
            raise OptunaReconciliationError(
                f"Optuna trial {number} constraint metadata conflicts with "
                "authoritative observation"
            )

    def _reconcile_orphans(self, authoritative_numbers: set[int]) -> None:
        for frozen in self.study.trials:
            if frozen.number in authoritative_numbers:
                continue
            if frozen.state is TrialState.RUNNING:
                self.study.tell(frozen.number, state=TrialState.FAIL)
                self._pending_trials.pop(f"{self.strategy_name}:{frozen.number}", None)
                self._orphaned_native_trials.add(frozen.number)
                continue
            raise OptunaReconciliationError(
                f"native Optuna trial {frozen.number} has terminal state "
                f"{frozen.state.name} but no authoritative trial record"
            )

    def _expected_parameters(self, record: TrialRecord) -> dict[str, float]:
        return {
            name: float(record.proposal.parameter_values[name].to(parameter.unit).value)
            for name, parameter in sorted(self.problem.parameter_space.parameters.items())
        }

    def _expected_executed_parameters(self, record: TrialRecord) -> dict[str, str]:
        return {
            name: str(record.proposal.parameter_values[name].to(parameter.unit).value)
            for name, parameter in sorted(self.problem.parameter_space.parameters.items())
        }

    def _expected_distributions(self) -> dict[str, BaseDistribution]:
        distributions: dict[str, BaseDistribution] = {}
        for name, parameter in sorted(self.problem.parameter_space.parameters.items()):
            if parameter.scale is ParameterScale.LOG and parameter.resolution is not None:
                raise ValueError(
                    f"Optuna does not support a quantized log distribution for parameter {name!r}"
                )
            distributions[name] = FloatDistribution(
                float(parameter.minimum),
                float(parameter.maximum),
                log=parameter.scale is ParameterScale.LOG,
                step=(float(parameter.resolution) if parameter.resolution is not None else None),
            )
        return distributions

    def _set_frozen_trial_user_attr(
        self, frozen: FrozenTrial, key: str, value: object
    ) -> None:
        """Repair crash-left metadata through Optuna's only available storage hook."""

        storage = getattr(self.study, "_storage", None)
        setter = getattr(storage, "set_trial_user_attr", None)
        internal_id = getattr(frozen, "_trial_id", None)
        if not callable(setter) or not isinstance(internal_id, int):
            raise OptunaReconciliationError(
                f"Optuna trial {frozen.number} cannot be repaired by this storage backend"
            )
        setter(internal_id, key, value)

    @staticmethod
    def _float_sequences_equal(
        actual: Sequence[object], expected: Sequence[float]
    ) -> bool:
        if len(actual) != len(expected):
            return False
        for raw, expected_value in zip(actual, expected, strict=True):
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                return False
            if not math.isclose(float(raw), expected_value, rel_tol=1e-12, abs_tol=1e-12):
                return False
        return True

    def _backend_checkpoint_data(self) -> dict[str, object]:
        return {
            "study_name": self.study_name,
            "sampler_name": self.sampler_name,
            "optuna_trial_count": len(self.study.trials),
            "storage_is_persistent": self.storage_url is not None,
            "orphaned_native_trials": sorted(self._orphaned_native_trials),
        }

    @staticmethod
    def _parse_trial_number(trial_id: str) -> int:
        prefix, separator, raw_number = trial_id.partition(":")
        if prefix != "bayesian" or not separator:
            raise ValueError(f"invalid Optuna trial ID: {trial_id}")
        try:
            number = int(raw_number)
        except ValueError as error:
            raise ValueError(f"invalid Optuna trial ID: {trial_id}") from error
        if number < 0:
            raise ValueError(f"invalid Optuna trial ID: {trial_id}")
        return number
