"""Scientific metrics from a QC-passed culture run.

Only two metrics in this slice, both direct readouts rather than fitted models:

- ``final_od750``: the matched end-point optical density (maximise);
- ``energy_kwh``: device-attested energy used during the hold (minimise).

The OD series is kept alongside so later slices can add growth-rate fitting
without re-reading evidence.
"""

from __future__ import annotations

from decimal import Decimal

from autonomous_lab.culture.evidence import CultureRunEvidence
from autonomous_lab.culture.models import ENERGY_UNIT
from autonomous_lab.culture.timeseries import TimeSeriesReconciliation
from autonomous_lab.domain.base import DomainModel
from autonomous_lab.domain.quantities import QuantityValue


class CultureAnalysisError(ValueError):
    """Analysis was asked to run on evidence that cannot support it."""


class ODPoint(DomainModel):
    expected_measurement_id: str
    elapsed_hours: Decimal
    od: Decimal


class CultureMetrics(DomainModel):
    final_od750: QuantityValue
    energy_kwh: QuantityValue
    od_series: tuple[ODPoint, ...]

    def objective_values(self) -> dict[str, QuantityValue]:
        return {"final_od750": self.final_od750, "energy_kwh": self.energy_kwh}


def analyse_culture(
    evidence: CultureRunEvidence, reconciliation: TimeSeriesReconciliation
) -> CultureMetrics:
    matched = reconciliation.matched()
    if not matched or reconciliation.entries[-1].status != "matched":
        raise CultureAnalysisError("analysis needs a matched final reading")
    series = tuple(
        ODPoint(
            expected_measurement_id=entry.expected_measurement_id,
            elapsed_hours=Decimal(
                str((entry.measurement.measured_at - evidence.hold_started_at).total_seconds())
            )
            / Decimal(3600),
            od=entry.measurement.raw_value,
        )
        for entry in matched
        if entry.measurement is not None
    )
    if len(evidence.telemetry) < 2:
        raise CultureAnalysisError("energy needs telemetry at both ends of the hold")
    first = evidence.telemetry[0].cumulative_energy.to(ENERGY_UNIT).value
    last = evidence.telemetry[-1].cumulative_energy.to(ENERGY_UNIT).value
    if last < first:
        raise CultureAnalysisError("device energy counter went backwards")
    return CultureMetrics(
        final_od750=QuantityValue(value=series[-1].od, unit="dimensionless"),
        energy_kwh=QuantityValue(value=last - first, unit=ENERGY_UNIT),
        od_series=series,
    )
