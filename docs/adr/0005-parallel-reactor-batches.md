# ADR 0005: Parallel reactor batches (stage 8, first slice)

- Status: accepted. Approved by the algae-sim session under Steven's delegation,
  2026-11-15. Implemented on branch `stage8-parallel-batches`.
- Date: 2026-11-15
- Requested by: Steven, via the algae-sim session ("yeah kick of stage 8's first slice")
- Scope: `STATE.md` → Next 1 only. Multi-campaign scheduling, pre-registration and
  independent confirmation are out of scope.

## Problem

A culture campaign today asks the optimiser for one condition, runs it on every
reactor in `culture_design.reactor_ids` as biological replicates, tells the result,
and repeats. A 9-reactor bank therefore spends 9 reactors on one condition per
36-hour round. Stage 8's first slice runs *k different conditions at once*, one per
reactor group, so a campaign needs fewer rounds.

## Decision

### Config

```yaml
optimization:
  strategy: bayesian
  batch_size: 3          # conditions per round; default 1 = today's behaviour
culture_design:
  reactor_ids: [r1, r2, r3, r4, r5, r6, r7, r8, r9]
  minimum_passing_replicates: 2   # now per group
```

- `batch_size` is in `optimization`, because it changes how the optimiser is asked.
  The default is 1. When it is 1, it is left out of the serialised document, so
  existing campaign hashes are unchanged (the same trick as `grid_levels`).
- **Reactor groups are derived, not declared.** `reactor_ids` is split, in order,
  into `batch_size` consecutive groups of equal size, so 9 reactors with
  `batch_size: 3` gives groups [r1–r3], [r4–r6], [r7–r9].
  - The config is refused at load unless `len(reactor_ids)` is a multiple of
    `batch_size`.
  - `minimum_passing_replicates` must be ≤ the group size and applies per group.
  - Slot *j* of every round runs on group *j*.
- Assay campaigns refuse `batch_size > 1`.
- `max_experiments` still counts conditions (experiments), not rounds. If the
  budget isn't a multiple of k, the last round is partial and its spare groups
  stay idle. `max_new_experiments` (the operator pause hook) stops at the end of
  the round in which it is reached, never mid-round.

### The round

1. **Ask k proposals**, then persist all k in one transaction before anything is
   dispatched. Because a round is atomic, a proposal's round and slot can always be
   derived from it: round = `suggestion_index // k`, slot = `suggestion_index % k`.
   The proposal schema doesn't change.
2. **Run the k experiments concurrently** with `asyncio.gather`. Each experiment
   uses the existing `CultureExperimentRunner` with a copy of the design limited to
   its group's reactors. Compile, validate, receipt, journal, QC and evidence all
   stay per experiment, unchanged.
3. **Each experiment is persisted, and its result told to the optimiser, as soon
   as it finishes.** The existing `_finish` path stays, including the durable
   optimiser outbox.
4. **Round barrier:** no proposal for round *r + 1* is asked until every experiment
   in round *r* has been told. That makes the proposals independent of the order
   in which experiments finish (see Seeds).
5. **Isolation:**
   - One group's rejection, QC failure or execution failure never cancels the other
     groups.
   - A QC failure or rejection is excluded, as today, and the campaign continues.
   - An execution failure still pauses the campaign for an operator, as today. The
     pause is deferred until the whole round has finished and been told, so the
     healthy groups complete and their evidence is kept.

### Shared devices

All k runners drive one controller and one sensor, so AutoLab adds two small
wrappers. Neither requires an adapter change.

- **Serialised controller:** a per-controller `asyncio.Lock` around every call.
  Adapters are not required to be safe under concurrent calls, and the calls are
  short (apply, telemetry, safe state).
- **Shared sensor session:** a reference-counted wrapper. `open()` opens on the
  first runner and `close()` closes after the last one, so one group finishing
  doesn't close the sensor under the others. Reads and self-tests go through the
  same lock.
- **Simulated clock:** `SteppedClock` currently jumps time forward on any
  `sleep_until`. With k concurrent runners, one runner could jump past another's
  sample time. In batch mode the stepped clock becomes a barrier: it advances to
  the earliest requested wake-up only once every live runner is waiting. With one
  runner this is exactly today's behaviour. `WallClock` needs no change.

### Optimiser

- **Optuna (`bayesian`)** in 4.9. Checked in the installed source:
  `_split_trials` puts RUNNING trials in the "above" set before the feasibility
  split. So with several objectives they never enter the below-set weights, and
  with `constraints_func` their missing constraint values are never read. The
  path is supported; it is experimental, like `constraints_func`. A unit test
  asks a k = 3 round on the 3-objective, constrained culture objective and checks
  for 3 distinct, in-bounds proposals and no warnings.
  - TPE gets `constant_liar=True` when `batch_size > 1`. It then treats running
    trials as pessimistic lies, so the k proposals in a round spread out instead
    of clustering.
  - `GPSampler` already conditions on RUNNING trials natively in 4.9, so it needs
    no option.
  - With `batch_size: 1` there are never running trials at ask time, and the
    option isn't set at all, so behaviour is identical by construction.
- **Random and grid:** proposals depend only on the suggestion index, so batching
  is trivial.
- **Replay on resume:** `OptunaStrategy._reconstruct_sampler_state` currently
  replays trial *i* with trials 0…*i − 1* complete. In batch mode, trial *i* was
  asked while the earlier trials of its own round were RUNNING. So the replay asks
  a whole round, with each trial's own seeded sampler, before telling any of it.
  `reconcile_pending` accepts several pending proposals, all in the final round.
  Its contiguity check becomes: terminal records plus pending proposals cover
  0…n − 1.

### Seeds (how they map in batch mode)

- The per-trial sampler seed is unchanged: `campaign seed + suggestion_index`. The
  advice to space seeds ≥ `max_experiments` apart still applies.
- **The start-up phase lengthens to a round boundary.** TPE and GP use random
  start-up until `startup_experiments` trials are *complete* at ask time. With
  k = 3 and `startup_experiments: 8`, rounds 0–2 (9 proposals) are random, not 8.
- **The first `startup_experiments` proposals are identical** to a sequential
  campaign with the same seed, because start-up draws depend only on the trial
  seed. Model-guided proposals differ, because they condition on running trials.
- **Determinism:** the Optuna study state is its trial list, and every trial gets
  a fresh, seeded sampler. With the round barrier, a run's proposals don't depend
  on which group finishes first. A simulated batch campaign is reproducible for a
  given seed, because asyncio's scheduling is deterministic and the barrier clock
  orders simulated time.

### Journal and resume

- The journal, run ids and resume all stay per experiment.
- On restart, the pending set is the final round's non-terminal experiments. The
  existing guard raises on more than one pending experiment, and it is relaxed to
  "all pending experiments are in the final round".
- Each pending experiment recovers exactly as today:
  - with no journal → FAILED (interrupted before dispatch);
  - with a journal → `runner.resume`, with the device confirming it kept holding.
- Pending experiments resume concurrently, with the same wrappers.
- Already-finished members of that round stay terminal. Any outbox rows not yet
  told are finalised by the existing `finalize_pending_observations`.

### Reporting

- `replicates_per_experiment` becomes the group size.
- `culture report` gains a `batching` block:
  - `batch_size`, `reactor_groups` and `rounds`;
  - `startup_experiments`, and `planned_random_startup`, which is the start-up
    rounded up to a whole round (9 at k = 3, startup 8);
  - `random_startup`, the actual count, from each experiment's observed trials
    when its round was asked;
  - per experiment: ordinal, round, slot, reactor group, state and
    `random_startup`.

## Tests (planned)

- **Batch of 3 on 9 reactors:**
  - each round sends 3 distinct conditions to the 3 groups;
  - replicates stay within their group;
  - every result is told before the next ask;
  - the campaign completes;
  - two runs with the same seed give identical proposals and evidence hashes.
- **A group fails mid-batch:**
  - a scripted controller error on one group's reactor: the other two groups
    complete with full evidence, are told, and then the campaign pauses;
  - a QC failure in one group: excluded, and the campaign continues.
- **Resume mid-batch:** interrupt during a round's hold (`keep_holding`), restart,
  and check that every in-flight group resumes and finishes. The Optuna replay
  audit passes, and the next round's proposals equal those of an uninterrupted run.
- **Config:**
  - refused: reactor count not divisible by k, minimum passing > group size,
    `batch_size > 1` on an assay campaign;
  - `batch_size: 1` leaves the hash unchanged.
- **Regression:** all existing tests pass unchanged. That is the "`batch_size: 1`
  is identical" check.

## Consequences and limits

- Group placement is fixed by slot, so a reactor-position effect would be
  confounded with slot. Seeded randomisation of slots to groups is a possible
  later option.
- A pause from an execution failure waits for the rest of the round, which can be
  up to a full culture duration. That is deliberate (isolation), and the report
  says why.
- Real devices: the serialised controller assumes call latency is small compared
  with the telemetry interval. That holds for algae-sim, and would need checking
  per adapter.
- Not in this slice: batches across campaigns or instruments, resource-conflict
  scheduling, varying k per round, and async (non-barrier) batching.
