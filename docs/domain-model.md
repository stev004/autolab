# Domain model

AutoLab represents each scientific stage as a separate immutable artifact.

| Artifact | Meaning | Important bindings |
|---|---|---|
| `ScientificObjective` | Objective directions and performance constraints | campaign |
| `ParameterSpace` | Named typed bounds, units, scales and optional resolution | campaign |
| `ExperimentProposal` | Optimiser-selected scientific conditions | campaign, trial, lineage |
| `ExperimentProtocol` | Materials, vessels, controls and ordered commands | exact proposal hash |
| `ValidationReport` | Deterministic issues and pass/reject decision | protocol, policy, capabilities |
| `ExecutableProtocol` | Receipt authorising one exact protocol | passing report hashes |
| `Measurement` | Immutable raw device observation | experiment, run, step, device |
| `QCResult` | Run-integrity decision and explicit checks | source measurement IDs |
| `ExperimentResult` | Links raw, QC and derived evidence | experiment and execution run |
| `OptimizationObservation` | Eligible objectives and constraint residuals | experiment and optimiser trial |
| `OptimizationState` | Seeded strategy checkpoint | campaign and strategy version |

`QuantityValue` is the persisted quantity representation. Pint is hidden behind its
conversion methods so serialized schemas do not depend on Pint internals. Canonical
laboratory units are microlitre, milligram/millilitre, minute, degree Celsius,
nanometre, GBP, and dimensionless.

## Proposal is not protocol

A proposal contains no commands. The V0 compiler expands one five-parameter recipe
into a plate batch containing three blanks, six calibration levels in triplicate,
four positive controls, and independent check-standard replicates. The protocol
captures source proposal identity/hash, a parameter snapshot, reagent formulation,
well layout, and stable step IDs.

## Raw and derived data

Raw measurements are append-only. Derived metrics reference their included and
explicitly excluded measurement IDs plus an algorithm version. Reanalysis creates
new evidence; it does not overwrite the readings or the earlier analysis.

## Identity and time

Persisted objects use UUIDs and timezone-aware UTC timestamps. Lineage is explicit;
row order, filenames, or wall-clock proximity never imply parentage.
