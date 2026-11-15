# AutoLab — state

Updated: 2026-11-15

## Stage
- Done: V0 (simulated assay loop), Stage 2 / v0.5 (human-in-the-loop, sensors, hardening), Stage 6 (biological culture, simulation and contract tests), culture contract v2.
- Stage 7 (research agents): first slice done. Stage 8: first slice (parallel reactor batches) built on branch `stage8-parallel-batches`, not yet merged. Not started: Stages 3, 4, 5.
- `main` = `26bfc8b` on GitHub `stev004/autolab` (CI passed, run 43365178603). Local `main` is ahead by `79ad855` (STATE only, not pushed). The branch has 363 tests passing (1 skipped: wheel test), with ruff and pyright clean.

## Current
- Culture campaigns (`autolab culture create/run/report/reconcile/flag-contamination`):
  - biological replicates and time-series OD;
  - contamination screens;
  - run journal with resume across restarts;
  - v2 named controlled variables (`temperature`, `light_intensity`, `ph` held via CO2), with resource metrics `energy_kwh` and `co2_g`.
  See `docs/culture.md`.
- Research agents (`autolab agent digest/interpret/draft/review`): a rule-based agent, plus Claude Code as an untrusted proposer via `.claude/skills/autolab-research-agent`. No model API calls, deliberately (cost). See `docs/agents.md`.
- Explicit grid designs: `optimization.grid_levels: {param: [values]}` (`780937b`), valid only with `strategy: grid`. Checked at load (every parameter, in range, on the resolution step, no repeats, ≤ `maximum_grid_size`, budget ≤ grid size). Order is lexicographic over sorted names. See `docs/culture.md` → Optimiser settings.
- Parallel reactor batches (branch `stage8-parallel-batches`; [ADR 0005](docs/adr/0005-parallel-reactor-batches.md)):
  - `optimization.batch_size: k` asks k conditions per round; each runs concurrently on its own reactor group (`reactor_ids` split evenly in order);
  - results are told before the next round; an execution failure pauses only after the round ends;
  - per-experiment journal and resume, with Optuna replay by round (TPE `constant_liar` for k > 1);
  - `culture report` → `batching` gives round, slot and group per experiment and the actual random start-up (9 at k = 3, startup 8). See `docs/culture.md` → Parallel reactor batches.
- Thresholds and the simulated biology are illustrative and unvalidated. No real hardware has been driven.

## Next
1. **Merge and push `stage8-parallel-batches`.** Waiting on Steven's go-ahead in this repo's session. Then the algae-sim session:
   - re-pins AutoLab and runs a regression check;
   - enlarges its reactor bank to 9;
   - adds a batch arm to its benchmark (k = 3 against sequential, 18 experiments), stating the extra random start-up trial.
2. Later stage 7 work: a model-backed agent (local model, or an API if affordable) behind `ResearchAgent`, an MCP server, and literature retrieval.
3. Housekeeping: delete merged local branches (`stage6-*`, `stage7-agents`, `culture-contract-v2`).

## Blockers
- None.

## Integrations
- **algae-sim** (`~/Documents/algae-sim`, GitHub-pinned dependency):
  - provides the `algae-sim-reactor` controller (`autonomous_lab.culture_controllers` entry point; integrated OD750 probe; `is_simulated=True`);
  - on contract v2 since algae-sim `fe01b0c`; pins AutoLab `26bfc8b` (checked in algae-sim `pyproject.toml`);
  - optimiser benchmark done (algae-sim `docs/benchmark.md`; 18 experiments × 3 replicates, 30 paired runs, seeds 1000 apart). AutoLab's TPE beat random search by +2.9 % hypervolume (better in 22/30, p < 0.001; no gain in best OD750) and the grid by +13.5 %. Simulator only;
  - per algae-sim's report (2026-11-11; not re-run from AutoLab), `configs/culture-algae-sim.yaml` (20 experiments × 3 reactors; temperature, light, pH; final_od750, energy_kwh, co2_g) runs to completion, and QC failures match its injected faults (control:temperature, growth_plausible, ph_low_trip with control:ph);
  - agreed v2 device facts: temperature 20–36 °C (heating only) with a 39 °C trip; light 0–500; pH 6.0–9.0 at 0.05 with trips at 5.5 and 9.8; resources energy (kWh) and co2 (g); safe state closes the CO2 valve.

  Decision record: `docs/adr/0004-culture-controlled-variables.md`.
