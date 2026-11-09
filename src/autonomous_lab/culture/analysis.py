"""Scientific metrics from a QC-passed culture experiment.

Per replicate, two direct readouts rather than fitted models:

- ``final_od750``: the matched end-point optical density (maximise);
- ``energy_kwh``: device-attested energy used by that reactor during the hold
  (minimise).

The experiment's value for each metric is the mean over the replicates that
passed QC, with the between-replicate standard deviation and the standard error
of the mean kept as the biological uncertainty. Replicates that failed QC are
excluded, never averaged in. The OD series is kept so growth-rate fitting can be
added later without re-reading evidence.
"""

from __future__ import annotations

import math
import statistics
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


class ReplicateMetrics(DomainModel):
    reactor_id: str
    final_od750: QuantityValue
    energy_kwh: QuantityValue
    od_series: tuple[ODPoint, ...]


class MetricSummary(DomainModel):
    mean: Decimal
    standard_deviation: Decimal | None
    standard_error: Decimal | None
    n: int


class CultureMetrics(DomainModel):
    final_od750: QuantityValue
    energy_kwh: QuantityValue
    replicates: tuple[ReplicateMetrics, ...]
    summaries: dict[str, MetricSummary]

    @property
    def od_series(self) -> tuple[ODPoint, ...]:
        """The first replicate's series (convenient for single-reactor designs)."""

        return self.replicates[0].od_series

    def objective_values(self) -> dict[str, QuantityValue]:
        return {"final_od750": self.final_od750, "energy_kwh": self.energy_kwh}

    def standard_errors(self) -> dict[str, Decimal]:
        return {
            name: summary.standard_error
            for name, summary in self.summaries.items()
            if summary.standard_error is not None
        }


def analyse_culture(
    evidence: CultureRunEvidence,
    reconciliations: dict[str, TimeSeriesReconciliation],
    passing_reactors: tuple[str, ...],
) -> CultureMetrics:
    if not passing_reactors:
        raise CultureAnalysisError("analysis needs at least one QC-passed replicate")
    replicates = tuple(
        _replicate_metrics(evidence, reactor_id, reconciliations[reactor_id])
        for reactor_id in passing_reactors
    )
    od = _summary([item.final_od750.value for item in replicates])
    energy = _summary([item.energy_kwh.value for item in replicates])
    return CultureMetrics(
        final_od750=QuantityValue(value=od.mean, unit="dimensionless"),
        energy_kwh=QuantityValue(value=energy.mean, unit=ENERGY_UNIT),
        replicates=replicates,
        summaries={"final_od750": od, "energy_kwh": energy},
    )


def _replicate_metrics(
    evidence: CultureRunEvidence, reactor_id: str, reconciliation: TimeSeriesReconciliation
) -> ReplicateMetrics:
    if reconciliation.entries[-1].status != "matched":
        raise CultureAnalysisError(f"{reactor_id}: analysis needs a matched final reading")
    series = tuple(
        ODPoint(
            expected_measurement_id=entry.expected_measurement_id,
            elapsed_hours=Decimal(
                str((entry.measurement.measured_at - evidence.hold_started_at).total_seconds())
            )
            / Decimal(3600),
            od=entry.measurement.raw_value,
        )
        for entry in reconciliation.matched()
        if entry.measurement is not None
    )
    telemetry = evidence.replicate(reactor_id).telemetry
    if len(telemetry) < 2:
        raise CultureAnalysisError(f"{reactor_id}: energy needs telemetry at both ends")
    first = telemetry[0].cumulative_energy.to(ENERGY_UNIT).value
    last = telemetry[-1].cumulative_energy.to(ENERGY_UNIT).value
    if last < first:
        raise CultureAnalysisError(f"{reactor_id}: device energy counter went backwards")
    return ReplicateMetrics(
        reactor_id=reactor_id,
        final_od750=QuantityValue(value=series[-1].od, unit="dimensionless"),
        energy_kwh=QuantityValue(value=last - first, unit=ENERGY_UNIT),
        od_series=series,
    )


def _summary(values: list[Decimal]) -> MetricSummary:
    mean = sum(values, Decimal(0)) / len(values)
    if len(values) < 2:
        return MetricSummary(mean=mean, standard_deviation=None, standard_error=None, n=1)
    sd = Decimal(str(statistics.stdev(float(item) for item in values)))
    return MetricSummary(
        mean=mean,
        standard_deviation=sd,
        standard_error=sd / Decimal(str(math.sqrt(len(values)))),
        n=len(values),
    )
