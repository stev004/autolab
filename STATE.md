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
1. **algae-sim migrates to contract v2** (algae-sim's own thread). Pin to `e9f8421`, update `algae_sim/autolab/_convert.py` and device core, then re-run its end-to-end campaign. AutoLab waits on this.
2. Once algae-sim is on v2: run `configs/culture-algae-sim.yaml` end to end against the real adapter. It optimises temperature, light and pH; objectives are final_od750, energy_kwh and co2_g.
3. **Stage 8, first slice: parallel reactor batches.**
   - batch `ask` of several conditions per round, each condition on its own reactor group (replicates within a group);
   - run the batch concurrently, then `tell` every result;
   - per-experiment journal and resume kept.

   Agreed with Steven; not started.
4. Later stage 7 work: a model-backed agent (local model, or an API if affordable) behind `ResearchAgent`, an MCP server, and literature retrieval.
5. Housekeeping: delete merged local branches (`stage6-*`, `stage7-agents`, `culture-contract-v2`).

## Blockers
- Step 2 waits on algae-sim's v2 adapter. Nothing else is blocked.

## Integrations
- **algae-sim** (`~/Documents/algae-sim`, GitHub-pinned dependency):
  - provides the `algae-sim-reactor` controller (`autonomous_lab.culture_controllers` entry point; integrated OD750 probe; `is_simulated=True`);
  - currently pinned to AutoLab `44e550e`, which is pre-v2;
  - agreed v2 device facts: temperature 20–36 °C (heating only) with a 39 °C trip; light 0–500; pH 6.0–9.0 at 0.05 with trips at 5.5 and 9.8; resources energy (kWh) and co2 (g); safe state closes the CO2 valve.

  Decision record: `docs/adr/0004-culture-controlled-variables.md`.
