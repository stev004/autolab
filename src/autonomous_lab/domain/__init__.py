"""Stable domain contracts shared by all AutoLab layers."""

from autonomous_lab.domain.base import DomainModel
from autonomous_lab.domain.enums import (
    CampaignState,
    ConstraintOperator,
    ExperimentState,
    ObjectiveDirection,
    QCStatus,
)
from autonomous_lab.domain.experiments import ExperimentProposal
from autonomous_lab.domain.objectives import MetricConstraint, ObjectiveMetric, ScientificObjective
from autonomous_lab.domain.parameters import ContinuousParameter, ParameterSpace
from autonomous_lab.domain.quantities import QuantityValue

__all__ = [
    "CampaignState",
    "ConstraintOperator",
    "ContinuousParameter",
    "DomainModel",
    "ExperimentProposal",
    "ExperimentState",
    "MetricConstraint",
    "ObjectiveDirection",
    "ObjectiveMetric",
    "ParameterSpace",
    "QCStatus",
    "QuantityValue",
    "ScientificObjective",
]

