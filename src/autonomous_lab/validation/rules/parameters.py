"""Proposal identity, parameter-space, and duplicate checks."""

from __future__ import annotations

from collections.abc import Mapping

from autonomous_lab.domain.parameters import ParameterSpace
from autonomous_lab.domain.protocols import ValidationIssue
from autonomous_lab.domain.quantities import QuantityValue
from autonomous_lab.provenance.hashing import canonical_sha256
from autonomous_lab.validation.rules.base import ValidationContext, error

RULE = "proposal_parameters"


def parameter_fingerprint(
    values: Mapping[str, QuantityValue],
    parameter_space: ParameterSpace,
) -> str:
    """Hash scientific conditions in declared units so equivalent units collide."""

    normalized = {
        name: values[name].to(definition.unit).model_dump(mode="python")
        for name, definition in sorted(parameter_space.parameters.items())
    }
    return canonical_sha256(normalized)


def validate(context: ValidationContext) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    proposal = context.proposal
    protocol = context.protocol
    if protocol.experiment_id != proposal.experiment_id:
        issues.append(
            error(
                "proposal.experiment_mismatch",
                "experiment_id",
                "protocol experiment_id does not match its source proposal",
                RULE,
            )
        )
    if protocol.source_proposal_id != proposal.proposal_id:
        issues.append(
            error(
                "proposal.id_mismatch",
                "source_proposal_id",
                "protocol source_proposal_id does not match its source proposal",
                RULE,
            )
        )
    expected_proposal_hash = canonical_sha256(proposal)
    if protocol.source_proposal_hash != expected_proposal_hash:
        issues.append(
            error(
                "proposal.hash_mismatch",
                "source_proposal_hash",
                "protocol is not bound to the exact source proposal",
                RULE,
                details={
                    "expected": expected_proposal_hash,
                    "actual": protocol.source_proposal_hash,
                },
            )
        )
    protocol_parameter_hash = _parameter_snapshot_hash(protocol.parameter_values)
    proposal_parameter_hash = _parameter_snapshot_hash(proposal.parameter_values)
    if protocol_parameter_hash != proposal_parameter_hash:
        issues.append(
            error(
                "proposal.parameter_snapshot_mismatch",
                "parameter_values",
                "protocol parameter snapshot differs from its source proposal",
                RULE,
            )
        )

    parameter_space = context.campaign.parameter_space
    expected_names = set(parameter_space.parameters)
    actual_names = set(protocol.parameter_values)
    for missing in sorted(expected_names - actual_names):
        issues.append(
            error(
                "parameter.missing",
                f"parameter_values.{missing}",
                f"required parameter {missing!r} is missing",
                RULE,
            )
        )
    for extra in sorted(actual_names - expected_names):
        issues.append(
            error(
                "parameter.unknown",
                f"parameter_values.{extra}",
                f"parameter {extra!r} is not in the campaign parameter space",
                RULE,
            )
        )

    compatible_values: dict[str, QuantityValue] = {}
    for name in sorted(expected_names & actual_names):
        value = protocol.parameter_values[name]
        definition = parameter_space.parameters[name]
        try:
            converted = value.to(definition.unit)
        except ValueError:
            issues.append(
                error(
                    "parameter.unit_incompatible",
                    f"parameter_values.{name}",
                    f"parameter {name!r} must be compatible with {definition.unit!r}",
                    RULE,
                    details={"actual_unit": value.unit, "expected_unit": definition.unit},
                )
            )
            continue
        compatible_values[name] = converted
        if not definition.minimum <= converted.value <= definition.maximum:
            issues.append(
                error(
                    "parameter.out_of_bounds",
                    f"parameter_values.{name}",
                    f"parameter {name!r} is outside its allowed bounds",
                    RULE,
                    details={
                        "value": str(converted.value),
                        "minimum": str(definition.minimum),
                        "maximum": str(definition.maximum),
                        "unit": definition.unit,
                    },
                )
            )
        if name in {
            "reagent_concentration",
            "reagent_volume",
            "sample_volume",
            "incubation_time",
        } and converted.value <= 0:
            issues.append(
                error(
                    "parameter.nonsensical_nonpositive",
                    f"parameter_values.{name}",
                    f"parameter {name!r} must be positive for an assay",
                    RULE,
                )
            )

    if len(compatible_values) == len(expected_names) and actual_names == expected_names:
        fingerprint = parameter_fingerprint(compatible_values, parameter_space)
        if fingerprint in context.historical_parameter_hashes:
            issues.append(
                error(
                    "experiment.duplicate_parameters",
                    "parameter_values",
                    "an experiment with equivalent parameter values already exists",
                    RULE,
                    details={"parameter_fingerprint": fingerprint},
                )
            )
    return tuple(issues)


def _parameter_snapshot_hash(values: Mapping[str, QuantityValue]) -> str:
    return canonical_sha256(
        {
            name: quantity.model_dump(mode="python")
            for name, quantity in sorted(values.items())
        }
    )
