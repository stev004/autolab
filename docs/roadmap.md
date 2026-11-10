# Roadmap

## V0 - simulated autonomous assay optimisation

Deliver one local closed loop with typed configuration, proposal compilation,
deterministic validation, simulated devices, raw measurements, integrity QC,
analysis, constrained multi-objective optimisation, SQLite provenance, pause/resume,
CLI reporting, and seeded end-to-end improvement tests.

## V0.5 / Stage 2 - human in the loop (implemented)

Export validated worklists and result-import templates. Persist grouped operator
acknowledgements, actual quantities, deviations, timestamps, immutable manual/CSV
measurement revisions, and a fixture-backed read-only sensor boundary before QC.

## Stage 3 - fixed fluidics

Add pump and colorimeter adapters, inventory/waste accounting, calibration expiry,
hardware interlocks, and explicit recovery from uncertain dispense state.

## Stage 4 - Cartesian liquid handling

Add labware geometry, deck state, collision-aware vendor adapters, plate-layout
randomisation and batched acquisition.

## Stage 5 - multimodal sensing

Add fluorescence, electrochemical, imaging and microscopy measurement schemas plus
modality-specific QC and artifact storage.

## Stage 6 - biological culture (implemented, simulation and contract tests)

Add long-running bioreactor state, sampling schedules, environmental control,
contamination handling and biological replicate semantics. See [culture](culture.md).

- **Environmental control:** a hold-conditions protocol, deterministic validation
  with hash-bound receipts, and a setpoint-accepting controller seam
  (`autonomous_lab.culture_controllers`) with acknowledgements and per-reactor
  safe state.
- **Sampling schedules:** regular or explicit time-series OD plans with tolerance
  windows and reconciliation.
- **Biological replicates:** the same conditions on several reactors in parallel,
  QC per replicate, a minimum-passing rule, and a replicate mean with its spread
  passed to the optimiser as uncertainty.
- **Contamination handling:** device and sensor indicators, OD-drop and
  growth-rate plausibility screens, and post-hoc operator flags that reports honour.
- **Long-running state:** a run journal committed as the culture happens, resume
  across AutoLab restarts (with the device confirming it kept holding), an optional
  detach that keeps the device holding, and operator `reconcile`.
- **Campaigns:** `experiment_type: culture`, `CultureCampaignService` sharing
  `CampaignLedger` with assay campaigns, and `autolab culture
  create/run/report/reconcile/flag-contamination`.

Not claimed: unattended physical culture, validated thresholds, or any real
bioreactor run. Batch proposals across reactors belong to stage 8.

## Stage 7 - research-agent layer (first slice implemented)

Add literature retrieval, structured hypothesis/campaign drafts and result
interpretation. Agents remain outside validation and device execution.
See [agents](agents.md).

- **Implemented:** the agent boundary (research goal, campaign draft, evidence digest,
  interpretation), a rule-based `TemplateResearchAgent`, a draft review gate using the
  same strict validation and trusted policy as hand-written campaigns, and the
  `autolab agent digest/interpret/draft/review` commands. A Claude Code skill lets
  Claude Code act as the research agent with no API calls.
- **Later:** a model-backed agent behind the same protocol, an MCP server, and
  literature retrieval.

## Stage 8 - reproducible discovery campaigns

Support multiple instruments and campaigns, scheduling, resource conflicts,
cross-campaign evidence, preregistered success criteria, independent confirmation and
reproducible scientific reports.

## Deliberately deferred

Web UI, cloud services, microservices, distributed queues, user accounts, arbitrary
protocol code, direct LLM control, general labware engines, autonomous uncertain-state
recovery, microscopy pipelines and unattended biological culture.
