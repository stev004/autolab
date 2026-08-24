# Physical-lab readiness without hardware

AutoLab V0.5 adds truthful asynchronous execution without claiming that mock or human
evidence is instrument evidence. The three modes share proposal compilation,
deterministic validation, expected measurement plans, QC, analysis, optimisation,
and storage. They differ only at execution and acquisition boundaries.

| Mode | Liquid/sample execution | Measurement source | Hidden assay world |
|---|---|---|---|
| `simulation` | simulated devices | simulated | used behind hardware abstraction |
| `human_manual` | acknowledged human worklist | manual entry, canonical CSV, or external lab | forbidden |
| `human_sensor` | acknowledged human worklist | read-only physical-sensor contract | forbidden |

`MockPhysicalSensor` carries `source_type=physical_sensor` plus explicit `is_mock`
metadata. The code path is the physical sensor boundary while the evidence still
truthfully says it came from a mock fixture.

## Durable human lifecycle

```text
PROPOSED → VALIDATED → QUEUED → AWAITING_EXECUTION
                                  ↓ operator starts
                               RUNNING
                                  ↓ completed acknowledgements
                         AWAITING_MEASUREMENTS
                                  ↓ complete plan reconciliation
                               MEASURED
                                  ↓ QC passes
                       QC_PASSED → ANALYSED → OBSERVED
```

Rejection, human failure/abort, and QC failure are terminal optimiser exclusions.
A worklist is generated only from a passing executable validation receipt. Starting,
completing, and acquiring are separate commands and may occur in separate processes.
The optimiser reconciles terminal history plus one durable pending proposal before it
accepts feedback or asks again, preventing duplicate observations after restart.

## Operator evidence and worklists

Every generated worklist has a canonical content hash and persisted Markdown,
preparation CSV, measurement-template CSV, and JSON model artifacts. It includes the
campaign/experiment/protocol identity, compiler version, execution mode, materials,
plate map, per-well preparation, grouped procedure, and expected measurement sheet.
Grouped acknowledgements expand back into per-protocol-step action events containing
the requested command, human executor identity, timestamps, actual quantities,
status/confidence, notes, deviations, and evidence references.

The current assay groups 83 protocol commands into five operator confirmations:
reagent dispense, sample/control addition, mixing, incubation, and measurement. An
operator may confirm, record a structured deviation, skip, fail, or abort. Quantity
and timing deviations can replace nominal values in analysis/cost accounting;
missing sample, suspected contamination, operator error, and device error fail QC.

## Canonical measurement CSV

The strict six-column form is:

```csv
sample_id,measurement_type,wavelength_nm,value,unit,timestamp
blank-01,absorbance,520,0.031,absorbance_unit,2026-08-14T18:42:00Z
```

An eight-column form adds `status,notes`; `status=unavailable` requires an explanation.
The importer is all-or-nothing. It rejects the complete batch for unexpected/missing
sample IDs, type or wavelength mismatch, duplicates, invalid units, malformed or
non-finite values, bad timestamps, malformed rows, or an incorrect header. Accepted
source bytes receive SHA-256 provenance.

Manual and imported submissions form immutable batches and revisions. A correction
selects prior expected-measurement IDs, supplies a correction reason, and appends a
new active revision. Original decimal value, operator, timestamp, notes,
source-file hash, batch hash, plan hash, and supersession link remain queryable. QC
and analysis consume only the active complete reconciliation.

## Calibration and sensor evidence

`CalibrationRecord` contains sensor and measurement identity, timestamp/expiry,
method, reference material, coefficients/parameters, optional raw artifact hash,
lifecycle status, supersession, and a canonical record hash. The sensor rejects a
missing, expired, invalidated, superseded, or mismatched calibration before returning
a reading. Capability manifests and raw sensor evidence embed calibration references
and hashes.

The first real optical adapter needs to implement only:

1. open/close the transport and expose stable device identity;
2. report measurement range, wavelength support, precision/resolution where known,
   versions, and calibration reference in `SensorCapabilities`;
3. perform and report a bounded self-test;
4. translate `MeasurementRequest` to one device read and return raw, timestamped,
   identity-bound `RawSensorMeasurement` evidence;
5. map timeout, disconnect, malformed payload, unsupported request, and calibration
   failures to the typed sensor errors.

It must not interpret the assay, run QC/analysis, know optimiser state/objectives,
control pumps, or access simulator coefficients. A real adapter therefore slots into
`MockPhysicalSensorSource`'s `ReadOnlySensor` dependency without changes above the
measurement-source layer.

## Honest limitations

- Human acknowledgement is self-reported evidence, not independent verification.
- Configured cost coefficients estimate costs; no balance, inventory, or purchasing
  system measures them.
- The mock sensor validates software lifecycle and provenance only; it says nothing
  about serial transports, optical stability, environmental effects, or real safety.
- V0.5 has no authentication, electronic signatures, lot/inventory tracking,
  plate-position verification, uncertainty propagation from certificates, or
  unattended physical interlocks.
- Calibration records are modelled and hashed, but V0.5 does not perform a real
  calibration protocol or certify reference materials.

These are deliberate constraints. Missing physical evidence stays missing and is
never replaced by simulation.
