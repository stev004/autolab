# AutoLab

AutoLab is a local, hardware-agnostic platform for reproducible closed-loop
experimental optimisation. V0.5 can execute the scientific loop in simulation,
pause for a human operator and external measurements, or acquire fixture-backed
measurements through the same read-only contract intended for a future physical
sensor. The optimiser cannot see the hidden response surface, and no optimiser or
research agent can issue a device command.

```text
objective → proposal → compiled protocol → validation receipt → execution
          ← optimiser observation ← analysis/QC ← raw measurements
```

The scientific proposal, protocol, validation report, execution events, raw
measurements, QC result, derived metrics, and optimiser observation are separate,
versioned evidence artifacts. A protocol is executable only while its content,
trusted validation policy, and current hardware capability hashes match its passing
receipt.

## Install

AutoLab requires Python 3.12 or newer and uses [uv](https://docs.astral.sh/uv/) for a
locked environment.

```bash
uv sync --extra dev
uv run autolab --help
```

Optuna's constrained multi-objective TPE strategy is available in the base install.
For the heavier experimental GP sampler, install `uv sync --extra dev --extra bayesian`;
AutoLab falls back to TPE when PyTorch is unavailable. Random and bounded grid
strategies remain available through the same adapter.

## Run V0

```bash
uv run autolab init --database data/autolab.sqlite3

# Compile and validate a deterministic midpoint proposal without executing it.
uv run autolab campaign validate configs/assay-reagent-reduction.yaml \
  --policy configs/validation-policy.yaml

# Create and run the seeded simulated campaign.
uv run autolab campaign create configs/assay-reagent-reduction.yaml \
  --database data/autolab.sqlite3
uv run autolab campaign run <campaign-id> --database data/autolab.sqlite3

uv run autolab campaign status <campaign-id> --database data/autolab.sqlite3
uv run autolab campaign report <campaign-id> --database data/autolab.sqlite3
uv run autolab experiment show <experiment-id> --database data/autolab.sqlite3
```

`campaign run` prints one compact progress line per completed experiment. Use
`--quiet` for scripted execution.

Use a small experiment budget while developing:

```bash
uv run autolab campaign run <campaign-id> --max-experiments 5
```

## Run the zero-hardware physical workflow

Create a manual campaign and advance it to the first durable human boundary:

```bash
uv run autolab campaign create configs/assay-reagent-reduction-manual.yaml \
  --database data/autolab.sqlite3
uv run autolab campaign run <campaign-id> --database data/autolab.sqlite3
uv run autolab experiment worklist <experiment-id> --database data/autolab.sqlite3
uv run autolab experiment execution start <experiment-id> \
  --operator-id steven --database data/autolab.sqlite3
uv run autolab experiment execution complete <experiment-id> \
  --operator-id steven --database data/autolab.sqlite3
uv run autolab experiment measurements enter <experiment-id> \
  --operator-id steven --database data/autolab.sqlite3
uv run autolab campaign run <campaign-id> --database data/autolab.sqlite3
```

`execution complete` is interactive by default; `--yes` confirms all grouped steps
for development dry-runs. Use `measurements enter` for manual entry, `measurements
status` to reconcile the plan, and `experiment deviations` to inspect recorded
departures. To use strict file import instead, select `measurement_source: csv_import`
in the campaign configuration and invoke `measurements import --submitted-by ...`.
The mock-sensor example replaces only the measurement command:

```bash
uv run autolab campaign create configs/assay-reagent-reduction-mock-sensor.yaml \
  --database data/autolab.sqlite3
uv run autolab experiment measurements acquire <experiment-id> \
  --operator-id steven --database data/autolab.sqlite3
```

Human modes never backfill absent evidence from the assay simulator. Each run may
stop after proposal, execution, or measurement ingestion; the SQLite ledger and
Optuna study reconcile when `campaign run` is invoked later. See
[Physical-lab readiness](docs/physical-lab-readiness.md) for source truth,
the canonical CSV format, state transitions, and adapter responsibilities.

## Develop

```bash
uv run pytest
uv run ruff check .
uv run pyright
```

The end-to-end tests use fixed seeds and verify that a resumed campaign preserves
the evidence chain and that optimisation improves feasible Pareto hypervolume over
its startup design.

## Physical hardware seam

The campaign runner depends on a backend factory and the orchestrator depends only
on typed device capabilities. A future adapter supplies a `DeviceRegistry`, executes
the existing command union, and returns device-local evidence fragments. The
backend's neutral finalizer composes those fragments into `ExecutionEvidence` and
rejects missing or conflicting facts before success is recorded; devices cannot
choose experiments or bypass the hash-bound validation receipt. Replacing the
simulated spectrophotometer therefore does not expose the optimiser—or a future
research agent—to motors, pumps, transports, manufacturer SDKs, or another device's
private state. See the
[hardware interface](docs/hardware-interface.md) for adapter obligations.

## Read next

- [Architecture](docs/architecture.md)
- [Physical-lab readiness](docs/physical-lab-readiness.md)
- [Scientific semantics](docs/scientific-semantics.md)
- [Protocol and validation](docs/protocol-and-validation.md)
- [Hardware interface](docs/hardware-interface.md)
- [Provenance](docs/provenance.md)
- [Roadmap](docs/roadmap.md)

AutoLab is research software. Human confirmation, a mock-sensor reading, or a
passing software validation receipt is not certification that a physical operation
occurred safely or correctly.
