# Session log

Newest first. Each entry: decisions · changes (commits) · open threads with resume points · user actions.

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
- **algae-sim v2 migration.** Owner: the algae-sim thread. Next action: pin AutoLab
  `e9f8421`, migrate `algae_sim/autolab/_convert.py` and device core, then re-run its
  end-to-end campaign. AutoLab waits; afterwards run `configs/culture-algae-sim.yaml`
  against it.
- **Stage 8 parallel batches.** Owner: an AutoLab session, once algae-sim is on v2.
  Scope is in `STATE.md` → Next 3.
- GitHub CI passed on `e9f8421` (run 42935650970).
- Merged local branches are still present; delete them when convenient.

**User actions**
- None required. Optionally delete the merged branches:
  `git branch -d stage6-culture-slice-a stage6-culture-slice-b stage6-completion stage7-agents culture-contract-v2`.
