# ADR 0001: Local modular monolith

- Status: accepted
- Date: 2026-08-14

## Decision

Build V0 as one typed Python application with SQLite and explicit internal module
boundaries.

## Rationale

The primary V0 risks are scientific semantics, safety gating, execution failure and
reproducibility—not horizontal scale. One process permits atomic evidence writes,
deterministic tests and straightforward debugging. Interfaces preserve future
extraction points if physical scheduling later requires separate workers.

## Consequences

No message broker, service discovery, cloud dependency or distributed transaction is
introduced. Campaign concurrency is intentionally limited. Long-running physical
work will later need leases and reconciliation without changing domain artifacts.
