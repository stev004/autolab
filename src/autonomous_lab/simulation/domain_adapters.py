"""Narrow conversion from simulator records to immutable domain measurements."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from autonomous_lab.domain.enums import MeasurementRole
from autonomous_lab.domain.measurements import Measurement
from autonomous_lab.simulation.assay_model import AssayBatch, RawAssayMeasurement


def simulated_measurement_identifier(
    item: RawAssayMeasurement,
    *,
    run_id: UUID,
    step_id: str,
    device_id: str,
) -> UUID:
    """Return the stable acquisition identity for present or missing data."""

    return uuid5(
        NAMESPACE_URL,
        ":".join(("autolab", item.measurement_id, str(run_id), step_id, device_id)),
    )


def measurements_from_simulated_batch(
    batch: AssayBatch,
    *,
    experiment_id: UUID,
    run_id: UUID,
    measured_at: datetime,
    step_id: str = "measure-absorbance",
    device_id: str = "simulated-spectrophotometer",
) -> tuple[Measurement, ...]:
    """Convert a complete raw batch into domain measurements.

    Missing/failed readings cannot be represented as successful ``Measurement``
    objects and therefore raise.  Their neutral raw records remain available for
    integrity QC and failure provenance.
    """

    missing = tuple(item.measurement_id for item in batch.measurements if item.absorbance is None)
    if missing:
        raise ValueError(f"cannot convert missing simulator readings: {', '.join(missing)}")

    converted: list[Measurement] = []
    for item in batch.measurements:
        if item.absorbance is None:
            raise AssertionError("missing readings were checked before conversion")
        converted.append(
            Measurement(
                measurement_id=simulated_measurement_identifier(
                    item,
                    run_id=run_id,
                    step_id=step_id,
                    device_id=device_id,
                ),
                experiment_id=experiment_id,
                run_id=run_id,
                step_id=step_id,
                device_id=device_id,
                vessel_id=item.well_id,
                role=MeasurementRole(item.role.value),
                analyte_level=Decimal(str(item.analyte_concentration)),
                replicate_index=item.replicate,
                raw_value=Decimal(str(item.absorbance)),
                unit="dimensionless",
                measured_at=measured_at,
                metadata={
                    "acquisition_index": item.acquisition_index,
                    "wavelength_nanometer": item.wavelength_nanometer,
                    "random_stream_id": item.stream_id,
                    "raw_simulator_measurement_id": item.measurement_id,
                    "simulator_model_id": batch.model_id,
                    "simulator_model_version": batch.model_version,
                    "simulator_config_hash": batch.simulator_config_hash,
                },
            )
        )
    return tuple(converted)
