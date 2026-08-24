# Experiment lifecycle

Campaign and experiment transitions are enforced domain state machines and recorded
as audit events.

```mermaid
stateDiagram-v2
    [*] --> Proposed
    Proposed --> Validated: passing receipt
    Proposed --> Rejected: validation errors
    Validated --> Queued
    Queued --> Running
    Running --> Failed: execution error / ambiguous state
    Running --> Measured: complete raw batch
    Measured --> QCFailed: integrity QC fails
    Measured --> QCPassed: integrity QC passes
    QCPassed --> Analysed
    Analysed --> Observed: optimiser tell exactly once
    Rejected --> [*]
    Failed --> [*]
    QCFailed --> [*]
    Observed --> [*]
```

```mermaid
stateDiagram-v2
    [*] --> Draft
    Draft --> Ready: config stored
    Ready --> Running
    Running --> Paused: boundary checkpoint
    Paused --> Running: reconcile and resume
    Running --> Completed: budget exhausted
    Running --> Failed: unrecoverable campaign error
```

An experiment rejected before execution is reported to the optimiser as rejected,
not assigned fabricated objective values. Execution and integrity-QC failures are
also terminal feedback but are not learned as scientific observations. A valid,
poor-performing recipe is observed with positive constraint residuals so constrained
optimisation can learn where infeasible scientific regions lie.

Resume occurs at experiment boundaries in V0. The campaign reconciles persisted
trials and checkpoints before asking for another suggestion, preventing duplicate
`tell` calls or accidental repeated physical work.
