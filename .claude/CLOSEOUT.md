# Closeout config — AutoLab

Closed-loop lab platform (propose → validate → execute → measure → learn); docs are the record for Steven's portfolio work and for any cold-start session.

## Doc map

| Fact changed | Owning file |
|---|---|
| Current stage, what's next, blockers (snapshot) | `STATE.md` |
| Stage plan / what each roadmap stage includes or defers | `docs/roadmap.md` |
| Module boundaries, dependency direction, trust boundaries | `docs/architecture.md` |
| Durable design decisions (why X over Y) | `docs/adr/NNNN-*.md` (new file per decision) |
| Biological culture: contract, QC, resume, replicates, CLI | `docs/culture.md` |
| Research agents: boundary, rule-based agent, Claude Code workflow | `docs/agents.md` (+ `.claude/skills/autolab-research-agent/SKILL.md`) |
| Device/sensor/controller adapter obligations | `docs/hardware-interface.md` |
| Assay validation rules and receipts | `docs/protocol-and-validation.md` |
| Experiment/campaign state machines | `docs/experiment-lifecycle.md` |
| Evidence storage, hashing, artifacts | `docs/provenance.md` |
| Human-in-the-loop / physical readiness | `docs/physical-lab-readiness.md` |
| Install, run, develop commands; status & limitations | `README.md` |
| Example configs and trusted policies | `configs/*.yaml` (comments explain values) |
| algae-sim integration (pins, contract version, adapter name) | `STATE.md` (Integrations) + `docs/culture.md` |

## State snapshot (MANDATORY — registered in StevOS sync registry as `STATE.md`)

Path: `STATE.md` at repo root. Sections: `Updated: YYYY-MM-DD` · Stage · Current · Next · Blockers · Integrations. Current truth only, ≤ ~55 lines; delete done items. Renaming it requires updating `~/Documents/StevOS/os/sync/REGISTRY.yaml` in the same session.

## Session log

Path: `docs/SESSION_LOG.md`
Entry format: `## YYYY-MM-DD — <one-line summary>` then bullets: decisions · changes (with commit hashes) · open threads with resume points · user actions needed. Newest first.

## Git policy

- Commit: work on a feature branch; Steven approves merges to `main`. Closeout commits docs on `main` only if the tree is otherwise clean.
- Push: only when Steven says so (AGENTS.md guardrail). Closeout push counts as instructed when Steven invokes /closeout.
- Protected: never force-push `main`; `uv run pytest`, `uv run ruff check .` and `uv run pyright` must pass before committing code.

## Quality bar (repo-specific examples)

- Slop: "worked on culture stuff, mostly done"
- Record: "Contract v2 on main e9f8421 (pushed); algae-sim adapter still on v1, pinned to 44e550e; next: algae-sim migrates `algae_sim/autolab/_convert.py` then re-pins"

## Never

- Edit the algae-sim repo from here (owned by its own session/thread); record integration facts only.
- Write secrets or API keys anywhere; none are used (no model API calls by design).
- Hand-edit `data/` (gitignored evidence, optimiser stores, agent drafts).
- Run `ruff format` across the whole repo (reformats unrelated files); format only touched files.
