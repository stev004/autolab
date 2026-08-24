# ADR 0003: Optuna behind an ask/tell adapter

- Status: accepted
- Date: 2026-08-14

## Decision

Use project-owned `OptimizationStrategy` ask/tell contracts. Random and bounded grid
implement baselines; an Optuna adapter supplies Bayesian/constrained multi-objective
search.

Derive a deterministic sampler seed for every suggestion from the campaign seed and
suggestion index. Each ask uses a fresh Optuna sampler against the complete persisted
study history. Persistent studies are loaded with that sampler through Optuna's public
API; in-memory studies are rebuilt from their public `FrozenTrial` history when no live
trial handles exist.

## Rationale

Established optimisation libraries should own acquisition mathematics, while the
campaign runner retains execution, QC eligibility and persistence. Ask/tell supports
external experiment lifetimes and deterministic reconciliation.

Optuna's RDB storage persists trials but not sampler RNG state. Reconstructing a
default long-lived sampler after process restart therefore restarts its random stream
and can repeat initial proposals. Per-suggestion seeds make acquisition a function of
the campaign seed, suggestion index and authoritative prior history, so pause/resume
is identical to an uninterrupted run without serializing private Optuna objects.

## Consequences

Optuna types do not cross the adapter. SQLite scientific records remain authoritative;
an optimiser-native store is a linked checkpoint artifact. The optional Bayesian
dependency can be replaced without changing protocols, devices or evidence schemas.

Reconciliation independently rebuilds each historical proposal in shadow studies
using the same per-suggestion seed and ordered observations. Parameter drift, sampler
version drift or an incompatible search history fails closed. Changing the seed
derivation or sampler construction is an optimiser strategy-version change.
