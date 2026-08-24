# Roadmap

## V0 — simulated autonomous assay optimisation

Deliver one local closed loop with typed configuration, proposal compilation,
deterministic validation, simulated devices, raw measurements, integrity QC,
analysis, constrained multi-objective optimisation, SQLite provenance, pause/resume,
CLI reporting, and seeded end-to-end improvement tests.

## V0.5 / Stage 2 — human in the loop (implemented)

Export validated worklists and result-import templates. Persist grouped operator
acknowledgements, actual quantities, deviations, timestamps, immutable manual/CSV
measurement revisions, and a fixture-backed read-only sensor boundary before QC.

## Stage 3 — fixed fluidics

Add pump and colorimeter adapters, inventory/waste accounting, calibration expiry,
hardware interlocks, and explicit recovery from uncertain dispense state.

## Stage 4 — Cartesian liquid handling

Add labware geometry, deck state, collision-aware vendor adapters, plate-layout
randomisation and batched acquisition.

## Stage 5 — multimodal sensing

Add fluorescence, electrochemical, imaging and microscopy measurement schemas plus
modality-specific QC and artifact storage.

## Stage 6 — biological culture

Add long-running bioreactor state, sampling schedules, environmental control,
contamination handling and biological replicate semantics.

## Stage 7 — research-agent layer

Add literature retrieval, structured hypothesis/campaign drafts and result
interpretation. Agents remain outside validation and device execution.

## Stage 8 — reproducible discovery campaigns

Support multiple instruments and campaigns, scheduling, resource conflicts,
cross-campaign evidence, preregistered success criteria, independent confirmation and
reproducible scientific reports.

## Deliberately deferred

Web UI, cloud services, microservices, distributed queues, user accounts, arbitrary
protocol code, direct LLM control, general labware engines, autonomous uncertain-state
recovery, microscopy pipelines and unattended biological culture.
