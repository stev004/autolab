# AutoLab — state

Updated: 2026-11-11

## Stage
- Done: V0 (simulated assay loop), Stage 2 / v0.5 (human-in-the-loop, sensors, hardening), Stage 6 (biological culture, simulation and contract tests), culture contract v2.
- Stage 7 (research agents): first slice done. Not started: Stages 3, 4, 5, 8.
- `main` = `e9f8421` (pushed to GitHub `stev004/autolab`). 341 tests pass locally; ruff and pyright clean. GitHub CI passed on `e9f8421`.

## Current
- Culture campaigns (`autolab culture create/run/report/reconcile/flag-contamination`):
  - biological replicates and time-series OD;
  - contamination screens;
  - run journal with resume across restarts;
  - v2 named controlled variables (`temperature`, `light_intensity`, `ph` held via CO2), with resource metrics `energy_kwh` and `co2_g`.
  See `docs/culture.md`.
- Research agents (`autolab agent digest/interpret/draft/review`): a rule-based agent, plus Claude Code as an untrusted proposer via `.claude/skills/autolab-research-agent`. No model API calls, deliberately (cost). See `docs/agents.md`.
- Thresholds and the simulated biology are illustrative and unvalidated. No real hardware has been driven.

## Next
1. **Stage 8, first slice: parallel reactor batches.**
   - batch `ask` of several conditions per round, each condition on its own reactor group (replicates within a group);
   - run the batch concurrently, then `tell` every result;
   - per-experiment journal and resume kept.

   Agreed with Steven; not started. algae-sim's reactor bank is the target rig.
2. Later stage 7 work: a model-backed agent (local model, or an API if affordable) behind `ResearchAgent`, an MCP server, and literature retrieval.
3. Housekeeping: delete merged local branches (`stage6-*`, `stage7-agents`, `culture-contract-v2`).

## Blockers
- None.

## Integrations
- **algae-sim** (`~/Documents/algae-sim`, GitHub-pinned dependency):
  - provides the `algae-sim-reactor` controller (`autonomous_lab.culture_controllers` entry point; integrated OD750 probe; `is_simulated=True`);
  - on contract v2 since algae-sim `fe01b0c` (local, not pushed yet; Steven decides), pinned to AutoLab `e9f8421`;
  - per algae-sim's report (2026-11-11; not re-run from AutoLab), `configs/culture-algae-sim.yaml` (20 experiments × 3 reactors; temperature, light, pH; final_od750, energy_kwh, co2_g) runs to completion, and QC failures match its injected faults (control:temperature, growth_plausible, ph_low_trip with control:ph);
  - agreed v2 device facts: temperature 20–36 °C (heating only) with a 39 °C trip; light 0–500; pH 6.0–9.0 at 0.05 with trips at 5.5 and 9.8; resources energy (kWh) and co2 (g); safe state closes the CO2 valve.

  Decision record: `docs/adr/0004-culture-controlled-variables.md`.
