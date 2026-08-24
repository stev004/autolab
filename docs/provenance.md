# Provenance and persistence

SQLite is the V0 system of record. WAL mode, foreign keys and explicit transactions
are enabled. Alembic owns schema evolution; `autolab init` creates the current schema
for a new local database.

The evidence chain includes campaigns, objective and parameter snapshots,
experiments and lineage, proposals, protocol versions, validation reports/issues,
execution runs and action events, measurements, QC runs/checks, derived metrics,
performance constraints, optimiser runs/observations/checkpoints, external artifacts,
and audit events.

## Reproduction envelope

Capture at least:

- original campaign text, raw-file SHA-256 and canonical parsed-configuration SHA-256;
- application version, Git commit, dirty-tree/content hash, Python/platform metadata
  and `uv.lock` hash;
- proposal strategy/version/trial, seed and parent experiments;
- protocol compiler/version and canonical protocol hash;
- validation rule versions, trusted policy snapshot/hash and capability snapshot/hash;
- device/adapter/calibration identity and action timestamps;
- simulator model/config hashes and independent random stream identifiers;
- raw measurement payloads and content hashes for external artifacts;
- QC/analysis methods, included/excluded measurement IDs, and optimiser checkpoint.

Scientific evidence is append-only. Corrections or reanalysis create new versioned
artifacts with explicit lineage. Campaign/experiment lifecycle rows may transition,
but each transition is also an audit event.

Large images and binary instrument exports belong in a content-addressed artifact
store. SQLite stores their relative URI, media type, byte count, SHA-256 and metadata.
