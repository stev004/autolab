# Scientific semantics

## Integrity QC is not assay performance

Run-integrity QC asks whether a batch is trustworthy enough to analyse. It checks
completeness, required replicates, blank contamination, broad control range,
detector saturation, computable calibration, check-standard recovery, drift and
explicit outlier flags.

Performance constraints ask whether a trustworthy recipe is useful: for example
`control_cv <= 0.05`, `calibration_r_squared >= 0.99`, and
`signal_to_noise >= 10`. A QC-passing recipe that violates these thresholds is a
valid infeasible observation. The optimiser should learn from it. A QC-failed run is
persisted but excluded from `tell` as an observation.

Outliers are flagged using a versioned method and never silently deleted. If an
analysis excludes a point, both included and excluded measurement IDs remain in its
artifact.

## Metrics

- Calibration is unweighted ordinary least squares on individual standard
  replicates; values are not averaged before fitting.
- Sensitivity is the calibration slope.
- Control CV is sample standard deviation divided by absolute control mean.
- SNR is `(control mean - blank mean) / pooled sample standard deviation`.
- Response uncertainty is replicate standard error for the difference of means;
  reported 95% expanded uncertainty uses `k=2`. It is not a full metrological budget.
- R² describes fit in the tested range. It is not alone proof of linearity or assay
  validity.

## Constraints and Pareto selection

Constraint adapters use residual `<= 0` as feasible. For an upper bound the residual
is `observed - threshold`; for a lower bound it is `threshold - observed`.

Multi-objective output is a feasible Pareto frontier. AutoLab does not silently turn
cost and reagent consumption into one weighted score. Selecting a production recipe
from the frontier requires an explicit decision rule or human choice.

## Interpretation limits

Technical replicates are not independent biological replicates. Sequential
optimisation can be confounded by drift, lot changes, carryover and plate position.
Success criteria and analysis methods should be fixed before the campaign; changing
them post hoc creates a new campaign or explicitly versioned analysis.
