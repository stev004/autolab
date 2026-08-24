# ADR 0002: Separate proposals from protocols

- Status: accepted
- Date: 2026-08-14

## Decision

An optimiser or research agent may emit only an `ExperimentProposal`. A registered,
deterministic compiler creates the `ExperimentProtocol`.

## Rationale

Scientific search values and physical operations have different trust and change
rates. Separating them prevents optimisation code from controlling hardware, makes
controls/replicates mandatory by construction, and allows one proposal to be compiled
for different validated hardware capabilities later.

## Consequences

Protocols bind the complete parameter snapshot and source proposal hash. Compiler
version is provenance. Any compilation change produces a different protocol hash and
requires validation again.
