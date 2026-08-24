"""Derive an external-measurement contract from a compiled protocol."""

from __future__ import annotations

from autonomous_lab.domain.enums import ControlType, MeasurementRole
from autonomous_lab.domain.protocols import ExperimentProtocol, MeasureAbsorbanceCommand
from autonomous_lab.measurement_ingestion.models import (
    ExpectedMeasurement,
    ExpectedMeasurementPlan,
    make_expected_plan,
)

_ROLE_BY_CONTROL = {
    ControlType.BLANK: MeasurementRole.BLANK,
    ControlType.STANDARD: MeasurementRole.STANDARD,
    ControlType.POSITIVE: MeasurementRole.CONTROL,
    ControlType.CHECK_STANDARD: MeasurementRole.CHECK_STANDARD,
}


def expected_plan_from_protocol(
    protocol: ExperimentProtocol,
    *,
    unit: str = "absorbance_unit",
) -> ExpectedMeasurementPlan:
    """Build stable expected IDs and sample metadata for external acquisition."""

    if protocol.protocol_hash is None:
        raise ValueError("protocol must have its validation-bound protocol_hash")
    measure_steps = [
        step for step in protocol.steps if isinstance(step.command, MeasureAbsorbanceCommand)
    ]
    if len(measure_steps) != 1:
        raise ValueError("V0.5 external ingestion requires exactly one absorbance step")
    step = measure_steps[0]
    controls_by_vessel = {
        vessel_id: (control, replicate_index)
        for control in protocol.controls
        for replicate_index, vessel_id in enumerate(control.vessel_ids)
    }
    entries: list[ExpectedMeasurement] = []
    for vessel_id in protocol.measurement_plan.vessel_ids:
        control_details = controls_by_vessel.get(vessel_id)
        if control_details is None:
            role = MeasurementRole.SAMPLE
            replicate_index = 0
            analyte_level = None
        else:
            control, replicate_index = control_details
            role = _ROLE_BY_CONTROL[control.control_type]
            analyte_level = control.nominal_level
        entries.append(
            ExpectedMeasurement(
                expected_measurement_id=f"{step.step_id}:{vessel_id}",
                sample_id=vessel_id,
                vessel_id=vessel_id,
                step_id=step.step_id,
                sample_role=role,
                measurement_type=protocol.measurement_plan.measurement_name,
                wavelength=protocol.measurement_plan.wavelength,
                unit=unit,
                analyte_level=analyte_level,
                replicate_index=replicate_index,
                required=True,
            )
        )
    return make_expected_plan(
        experiment_id=protocol.experiment_id,
        protocol_id=protocol.protocol_id,
        protocol_hash=protocol.protocol_hash,
        entries=tuple(entries),
        created_at=protocol.created_at,
    )
