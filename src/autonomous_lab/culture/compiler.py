"""Translate a proposal (scientific values) into an immutable culture protocol."""

from __future__ import annotations

from uuid import UUID

from autonomous_lab.culture.models import (
    CULTURE_PARAMETERS,
    LIGHT_PARAMETER,
    TEMPERATURE_PARAMETER,
    CultureDesign,
    CultureProtocol,
    CultureReplicate,
    HoldConditionsCommand,
    SamplePoint,
    TimeSeriesMeasurementPlan,
)
from autonomous_lab.domain.experiments import ExperimentProposal
from autonomous_lab.domain.quantities import QuantityValue
from autonomous_lab.provenance.hashing import canonical_sha256


class CultureCompilationError(ValueError):
    """The proposal cannot be expressed as a culture protocol at all."""


class CultureProtocolCompiler:
    """The only component that turns culture conditions into a device-neutral recipe.

    Deterministic: the same proposal and design always yield the same plan, which
    lets validation recompile and compare rather than trust the submitted plan.
    """

    name = "culture_hold_compiler"
    version = "1.0.0"

    @property
    def identity(self) -> str:
        return f"{self.name}@{self.version}"

    def compile(self, proposal: ExperimentProposal, design: CultureDesign) -> CultureProtocol:
        values = proposal.parameter_values
        if frozenset(values) != CULTURE_PARAMETERS:
            raise CultureCompilationError(
                f"culture proposals need exactly {sorted(CULTURE_PARAMETERS)}; got {sorted(values)}"
            )
        replicates = tuple(
            CultureReplicate(
                replicate_index=index,
                hold=HoldConditionsCommand(
                    reactor_id=reactor_id,
                    temperature_setpoint=values[TEMPERATURE_PARAMETER],
                    light_intensity=values[LIGHT_PARAMETER],
                    duration=design.duration,
                ),
                measurement_plan=build_sampling_plan(
                    design, reactor_id=reactor_id, experiment_key=str(proposal.experiment_id)
                ),
            )
            for index, reactor_id in enumerate(design.reactor_ids)
        )
        protocol = CultureProtocol(
            experiment_id=proposal.experiment_id,
            source_proposal_id=proposal.proposal_id,
            source_proposal_hash=canonical_sha256(proposal),
            parameter_values=dict(values),
            compiler_name=self.name,
            compiler_version=self.version,
            design_hash=canonical_sha256(design),
            replicates=replicates,
            minimum_passing_replicates=design.required_passing_replicates,
            telemetry_interval=design.telemetry_interval,
        )
        return protocol.model_copy(update={"protocol_hash": protocol.compute_hash()})


def build_sampling_plan(
    design: CultureDesign, *, reactor_id: str, experiment_key: str
) -> TimeSeriesMeasurementPlan:
    """One plan entry per scheduled reading for one reactor.

    The schedule comes from ``CultureDesign.offsets_seconds``: a regular interval
    that always includes the end point, or explicit offsets. There is no t=0
    reading; an inoculation reading is a separate concern.
    """

    points = tuple(
        SamplePoint(
            expected_measurement_id=f"od-{index:03d}",
            offset=QuantityValue(value=offset, unit="second"),
            tolerance=design.sample_tolerance,
            measurement_type=design.measurement_type,
            unit=design.measurement_unit,
            wavelength=design.wavelength,
        )
        for index, offset in enumerate(design.offsets_seconds(), start=1)
    )
    # Deterministic plan ID so a recompiled plan hashes identically.
    plan_seed = canonical_sha256(
        {"experiment": experiment_key, "reactor": reactor_id, "design": canonical_sha256(design)}
    )
    return TimeSeriesMeasurementPlan(
        plan_id=UUID(plan_seed[:32]),
        sample_id=reactor_id,
        points=points,
    )
