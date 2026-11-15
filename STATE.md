# AutoLab — state

Updated: 2026-11-15

## Stage
- Done: V0 (simulated assay loop), Stage 2 / v0.5 (human-in-the-loop, sensors, hardening), Stage 6 (biological culture, simulation and contract tests), culture contract v2.
- Stage 7 (research agents): first slice done. Not started: Stages 3, 4, 5, 8.
- `main` = `26bfc8b` (pushed to GitHub `stev004/autolab`). 350 tests pass locally (1 skipped: wheel test); ruff and pyright clean. GitHub CI passed on `26bfc8b` (run 43365178603).

## Current
- Culture campaigns (`autolab culture create/run/report/reconcile/flag-contamination`):
  - biological replicates and time-series OD;
  - contamination screens;
  - run journal with resume across restarts;
  - v2 named controlled variables (`temperature`, `light_intensity`, `ph` held via CO2), with resource metrics `energy_kwh` and `co2_g`.
  See `docs/culture.md`.
- Research agents (`autolab agent digest/interpret/draft/review`): a rule-based agent, plus Claude Code as an untrusted proposer via `.claude/skills/autolab-research-agent`. No model API calls, deliberately (cost). See `docs/agents.md`.
- Explicit grid designs: `optimization.grid_levels: {param: [values]}` (`780937b`), valid only with `strategy: grid`. Checked at load (every parameter, in range, on the resolution step, no repeats, ≤ `maximum_grid_size`, budget ≤ grid size). Order is lexicographic over sorted names. See `docs/culture.md` → Optimiser settings.
- Thresholds and the simulated biology are illustrative and unvalidated. No real hardware has been driven.

## Next
1. **Stage 8, first slice: parallel reactor batches.**
   - batch `ask` of several conditions per round, each condition on its own reactor group (replicates within a group);
   - run the batch concurrently, then `tell` every result;
   - per-experiment journal and resume kept.

   Agreed with Steven; not started. algae-sim's reactor bank is the target rig.
2. **Optimiser benchmark on algae-sim** (Steven approved 2026-11-13). Owner: the algae-sim session, which builds the runner in algae-sim; AutoLab's part (`grid_levels`) is done.
   - arms: bayesian (Optuna TPE), random, grid `temperature [22,28,34] × light_intensity [60,180,400] × ph [6.5,8.0]`; 20–30 seeds each; 3 replicates;
   - the grid arm's budget is 18 (its grid size), not 20;
   - seeds must be spaced ≥ `max_experiments` apart (e.g. 1000·k). See Optimiser settings in `docs/culture.md` for why.
   AutoLab moves next only if the benchmark needs another change.
3. Later stage 7 work: a model-backed agent (local model, or an API if affordable) behind `ResearchAgent`, an MCP server, and literature retrieval.
4. Housekeeping: delete merged local branches (`stage6-*`, `stage7-agents`, `culture-contract-v2`).

## Blockers
- None.

## Integrations
- **algae-sim** (`~/Documents/algae-sim`, GitHub-pinned dependency):
  - provides the `algae-sim-reactor` controller (`autonomous_lab.culture_controllers` entry point; integrated OD750 probe; `is_simulated=True`);
  - on contract v2 since algae-sim `fe01b0c`. Its pin was `e9f8421`; on 2026-11-13 the algae-sim session was told to move it to `780937b`/`26bfc8b` (same code) for `grid_levels`. Not confirmed from here;
  - per algae-sim's report (2026-11-11; not re-run from AutoLab), `configs/culture-algae-sim.yaml` (20 experiments × 3 reactors; temperature, light, pH; final_od750, energy_kwh, co2_g) runs to completion, and QC failures match its injected faults (control:temperature, growth_plausible, ph_low_trip with control:ph);
  - agreed v2 device facts: temperature 20–36 °C (heating only) with a 39 °C trip; light 0–500; pH 6.0–9.0 at 0.05 with trips at 5.5 and 9.8; resources energy (kWh) and co2 (g); safe state closes the CO2 valve.

  Decision record: `docs/adr/0004-culture-controlled-variables.md`.
