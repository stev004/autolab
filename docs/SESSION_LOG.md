# Session log

Newest first. Each entry: decisions · changes (commits) · open threads with resume points · user actions.

## 2026-11-13 — grid_levels for the algae-sim optimiser benchmark; README authorship line

Logged 2026-11-15 04:21 GMT. Agent-built and Steven-reviewed. It was directed by the
algae-sim session ("Algae-sim repo assessment") through cross-session messages.
Steven confirmed the merge, the push and the README line directly in this session.

**Decisions (Steven)**
- Benchmark approved: AutoLab's optimiser vs random vs grid on algae-sim, 20–30 seeds,
  3 replicates, with best-so-far curves and two pages including limits. The algae-sim
  session owns the runner.
- Option (i) approved: an explicit `optimization.grid_levels` setting, in lexicographic
  order with no shuffle. The benchmark grid is temperature [22,28,34] × light
  [60,180,400] × ph [6.5,8.0] = 18 points.
- Merge `780937b` to `main` and push ("Yes, merge and push"). Add the AI-authorship
  sentence to the README ("Commit and push it").

**Findings (from code; sent to the algae-sim session)**
- `bayesian` = Optuna 4.9 constrained multivariate TPE (MOTPE for several objectives).
  It uses GP only if torch is installed, and torch is absent in AutoLab's venv.
- Per-trial sampler seed = `seed + index`: adjacent campaign seeds share start-up
  points. This was verified: seed 7's points 2–4 = seed 8's points 1–3. Space seeds
  ≥ `max_experiments` apart.
- `random` is uniform over resolution steps (hash-seeded). Start-up is not Sobol or LHS.
- Before `grid_levels`, a grid enumerated every resolution step: about 2.1M points for
  the demo config, refused at load.
- Scoring: `hypervolume_2d` exists, with no 3-D version. Recommended an external
  3-objective hypervolume with a fixed reference point from declared bounds, plus
  best-so-far OD.
- Headless runs: the `CultureCampaignService` Python API (pattern in
  `tests/e2e/test_optimization_benchmark.py`), with per-experiment results from
  `optimizer_observations`.

**Changes (commits on `main`, pushed; CI passed on `26bfc8b`, run 43365178603)**
- `780937b` `optimization.grid_levels`:
  - config and load-time validation in `src/autonomous_lab/campaigns/config.py`;
  - wiring in `campaigns/lifecycle.py` (`_grid_levels` → `GridSearchStrategy(levels=)`);
  - the key is omitted when unset, so existing configs keep their canonical hash
    (checked);
  - tests in `tests/e2e/test_culture_campaign.py`: the 18-point grid end to end, and
    7 load failures.
  - 350 tests pass.
- `26bfc8b` README: the AI-authorship sentence, last bullet of "Status and limitations".
- Closeout: `STATE.md`, `docs/culture.md` (new Optimiser settings section; fixed the
  stale "once its adapter is migrated" line), and a new doc-map row in
  `.claude/CLOSEOUT.md`.

**Open threads (resume points)**
- **Benchmark.** Owner: the algae-sim session, which moves next. It moves algae-sim's
  pin to `780937b`/`26bfc8b`, then runs the arms. The grid arm has a budget of 18.
  AutoLab acts only if it asks for another change.
- **Stage 8 parallel batches.** Still next for AutoLab and unstarted. Scope is in
  `STATE.md` → Next 1.
- Merged local branches `stage6-*`, `stage7-agents` and `culture-contract-v2` are
  still present (`culture-grid-levels` was deleted).

**User actions**
- None required.

## 2026-11-11 — Stage 6 complete, stage 7 first slice, culture contract v2; closeout bootstrap

Covers 2026-11-09 → 2026-11-11. Agent-built, Steven-reviewed. It ran alongside the
algae-sim session, coordinated through the shared notes folder (outside the repo).

**Decisions (Steven)**
- Commit the uncommitted v0.5 hardening work on `main` first, then build stage 6 as
  slices A (contracts + in-memory runner) and B (campaign, persistence, CLI). Then
  "finish stage 6" as defined by the roadmap: replicates, contamination, schedules and
  long-running state.
- Stage 7 without paid API calls: build the agent boundary with a rule-based agent
  (option 1) and use Claude Code as the agent (option 2). A hosted-model agent is
  deferred for cost.
- Agreed order: stage 7 → culture contract v2 (controlled variables, pH via CO2) →
  algae-sim migrates its adapter → stage 8 (parallel reactor batches).
- pH is held by the device via CO2 dosing, not an independent knob; nitrate is out of
  scope. Ranges come from the device manifest: algae-sim's review corrected them to
  20–36 °C with a 39 °C trip, light 0–500, pH trips 5.5/9.8.
- Merges to `main` and pushes happen only on Steven's go-ahead. He approved each
  merge, and the push of `e9f8421`.

**Changes (commits on `main`)**
- `9ab20cf` v0.5 hardening: wheel migrations, evidence freeze/locking, CSV and decimal
  limits, sensor lifecycle timeouts.
- `9b44897` stage 6 slice A: the `autonomous_lab.culture` package.
- `b40e651` stage 6 slice B: `CultureCampaignService`, the `CampaignLedger` extraction
  (assay path unchanged), and `autolab culture` CLI.
- `44e550e` stage 6 completion: replicates, contamination screens, schedules, run
  journal, resume, reconcile. Also fixed safe-state results missing from evidence on a
  refused setpoint.
- `df59771` stage 7 first slice: the agent boundary, `TemplateResearchAgent`,
  `agent_gateway`, `autolab agent` CLI and the Claude Code skill.
- `e9f8421` culture contract v2 ([ADR 0004](adr/0004-culture-controlled-variables.md)).
  Also fixed a per-replicate metric lookup.
- Closeout commit: added `.claude/CLOSEOUT.md`, `STATE.md`, this log and ADR 0004, and
  a "Reference adapter: algae-sim" section in `docs/culture.md`.

**Open threads (resume points)**
- algae-sim v2 migration: done in algae-sim `fe01b0c`, pinned to `e9f8421`, local and
  not pushed. Its end-to-end run of `configs/culture-algae-sim.yaml` completes, with QC
  catching exactly its injected faults (algae-sim's report; not re-run from AutoLab).
  Pushing algae-sim is Steven's call, in that repo.
- **Stage 8 parallel batches.** Owner: the next AutoLab session; unblocked. Scope is in
  `STATE.md` → Next 1.
- GitHub CI passed on `e9f8421` (run 42935650970).
- Merged local branches are still present; delete them when convenient.

**User actions**
- None required. Optionally delete the merged branches:
  `git branch -d stage6-culture-slice-a stage6-culture-slice-b stage6-completion stage7-agents culture-contract-v2`.
