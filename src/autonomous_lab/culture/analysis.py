"""Scientific metrics from a QC-passed culture experiment.

Per replicate, direct readouts rather than fitted models:

- ``final_od750``: the matched end-point optical density (maximise);
- one metric per device consumption counter (``RESOURCE_METRICS``): ``energy_kwh``
  from ``energy`` and ``co2_g`` from ``co2``, the device-attested use during the
  hold (minimise).

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
from autonomous_lab.culture.models import RESOURCE_METRICS
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
    #: Resource metrics, e.g. {"energy_kwh": ..., "co2_g": ...}.
    resources: dict[str, QuantityValue]
    od_series: tuple[ODPoint, ...]

    def value(self, metric: str) -> QuantityValue:
        return self.final_od750 if metric == "final_od750" else self.resources[metric]


class MetricSummary(DomainModel):
    mean: Decimal
    standard_deviation: Decimal | None
    standard_error: Decimal | None
    n: int


class CultureMetrics(DomainModel):
    final_od750: QuantityValue
    resources: dict[str, QuantityValue]
    replicates: tuple[ReplicateMetrics, ...]
    summaries: dict[str, MetricSummary]

    @property
    def energy_kwh(self) -> QuantityValue:
        return self.resources["energy_kwh"]

    @property
    def od_series(self) -> tuple[ODPoint, ...]:
        """The first replicate's series (convenient for single-reactor designs)."""

        return self.replicates[0].od_series

    def objective_values(self) -> dict[str, QuantityValue]:
        return {"final_od750": self.final_od750, **self.resources}

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
    summaries = {"final_od750": _summary([item.final_od750.value for item in replicates])}
    resources: dict[str, QuantityValue] = {}
    # Only resources every passing replicate reported, so means compare like with like.
    shared: set[str] = set(replicates[0].resources)
    for item in replicates[1:]:
        shared &= set(item.resources)
    for metric in sorted(shared):
        unit = replicates[0].resources[metric].unit
        summary = _summary([item.resources[metric].value for item in replicates])
        summaries[metric] = summary
        resources[metric] = QuantityValue(value=summary.mean, unit=unit)
    return CultureMetrics(
        final_od750=QuantityValue(value=summaries["final_od750"].mean, unit="dimensionless"),
        resources=resources,
        replicates=replicates,
        summaries=summaries,
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
        raise CultureAnalysisError(f"{reactor_id}: resource use needs telemetry at both ends")
    resources: dict[str, QuantityValue] = {}
    for resource in sorted(evidence.controller_capabilities.resources):
        if resource not in RESOURCE_METRICS:
            continue
        metric, unit = RESOURCE_METRICS[resource]
        first = telemetry[0].consumption.get(resource)
        last = telemetry[-1].consumption.get(resource)
        if first is None or last is None:
            raise CultureAnalysisError(f"{reactor_id}: {resource} counter missing at an end")
        used = last.to(unit).value - first.to(unit).value
        if used < 0:
            raise CultureAnalysisError(f"{reactor_id}: device {resource} counter went backwards")
        resources[metric] = QuantityValue(value=used, unit=unit)
    return ReplicateMetrics(
        reactor_id=reactor_id,
        final_od750=QuantityValue(value=series[-1].od, unit="dimensionless"),
        resources=resources,
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
