"""Translate a proposal (scientific values) into an immutable culture protocol."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from autonomous_lab.culture.models import (
    CULTURE_PARAMETERS,
    LIGHT_PARAMETER,
    TEMPERATURE_PARAMETER,
    CultureDesign,
    CultureProtocol,
    HoldConditionsCommand,
    SamplePoint,
    TimeSeriesMeasurementPlan,
    seconds,
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
        hold = HoldConditionsCommand(
            reactor_id=design.reactor_id,
            temperature_setpoint=values[TEMPERATURE_PARAMETER],
            light_intensity=values[LIGHT_PARAMETER],
            duration=design.duration,
        )
        protocol = CultureProtocol(
            experiment_id=proposal.experiment_id,
            source_proposal_id=proposal.proposal_id,
            source_proposal_hash=canonical_sha256(proposal),
            parameter_values=dict(values),
            compiler_name=self.name,
            compiler_version=self.version,
            design_hash=canonical_sha256(design),
            hold=hold,
            measurement_plan=build_sampling_plan(
                design, experiment_key=str(proposal.experiment_id)
            ),
        )
        return protocol.model_copy(update={"protocol_hash": protocol.compute_hash()})


def build_sampling_plan(design: CultureDesign, *, experiment_key: str) -> TimeSeriesMeasurementPlan:
    """One reading every ``sampling_interval``, always including the end point.

    The first reading is at one interval, not at t=0: an inoculation reading is a
    separate concern (biological replicate and inoculum semantics are later work).
    """

    duration = seconds(design.duration)
    interval = seconds(design.sampling_interval)
    offsets: list[Decimal] = []
    current = interval
    while current < duration:
        offsets.append(current)
        current += interval
    offsets.append(duration)
    points = tuple(
        SamplePoint(
            expected_measurement_id=f"od-{index:03d}",
            offset=QuantityValue(value=offset, unit="second"),
            tolerance=design.sample_tolerance,
            measurement_type=design.measurement_type,
            unit=design.measurement_unit,
            wavelength=design.wavelength,
        )
        for index, offset in enumerate(offsets, start=1)
    )
    # Deterministic plan ID so a recompiled plan hashes identically.
    plan_seed = canonical_sha256({"experiment": experiment_key, "design": canonical_sha256(design)})
    return TimeSeriesMeasurementPlan(
        plan_id=UUID(plan_seed[:32]),
        sample_id=design.reactor_id,
        points=points,
    )
