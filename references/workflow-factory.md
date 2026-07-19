# Workflow Factory

The factory turns a workflow brief into an immutable, certifiable Pi harness
version. It does not ask a model to decide whether its own work passed.

Conditional graphs are compiled only for specialized schema `1.1` or `1.2`
runtimes. Use `1.2` when the same workflow also declares peers.
The shared generic mutation runner never claims to execute arbitrary branches.
Specialized entrypoints call the compiled evaluator between stage artifacts and
remain responsible for domain actions and domain smoke certification.

Bounded peer collaboration is compiled only for specialized schema `1.2`
runtimes. The factory emits a digest-bound peer contract, typed exchange
schemas, and a deterministic exchange validator. The specialized entrypoint
still owns the transport adapter, pending-message registration, aggregate
message budget, response quorum, cancellation, durable reconciliation, and
domain verification.

## Build and QA contract

1. Run the task directly once. Record correctness, wall time, model calls,
   context size, retries, and cost; this is the baseline.
2. Draft a blueprint conforming to `schemas/workflow-blueprint.schema.json`.
3. Present the ordered stages with each stage's input, output, verifier, model,
   and efficiency budget. Compile only after explicit approval of the canonical
   blueprint digest.
4. Implement and QA stages in order. Each stage must pass its smallest
   representative fixture and budget before the next stage starts.
5. Reuse passing checkpoints unless an input digest changes. Never rerun the
   completed prefix merely because a later stage failed.
6. Run one final end-to-end fixture and compare it with the direct baseline.
   Reject overhead that buys no declared reliability gain.
7. Give every acceptance criterion a stable ID and at least one argv-based
   mechanical verifier. Use a semantic judge only for irreducibly semantic
   criteria.
8. Compile into `versions/<semver>/`; never edit a promoted version in place.
9. Keep peer protocols, conditional graphs, trackers, ledgers, replay
   promotion, and resume machinery out until the workflow proves it needs them.

## Certification states

- `failed`: at least one required gate failed.
- `static_ready`: blueprint and compiled bundle pass structural gates; no model
  execution has been proven.
- `fixture_certified`: the generated fixture passed approval-negative,
  positive end-to-end, missing-input, context-overflow, and seal-tamper gates.

Static gates also include `pi_protocol_regression` (the shared
`run_verifiers()`/`run_supervisor_commands()` fail closed on a zero exit code
paired with an aborted/errored Pi JSONL stop reason) and, for `specialized`
runtimes only, `specialized_pi_invocation_guard` plus its own
`pi_invocation_guard_regression` fixture check: every script that shells out
to Pi directly must either import the shared fail-closed event-stream helper
or contain its own explicit aborted/error `stopReason` check, or certification
fails. `generic_mutation` runtimes are out of scope for this guard because
they always route through the already-tested shared runner.

`fixture_certified` is promotion-eligible but not itself promotable. Domain
adversarial fixtures and a paired replay corpus must be reviewed. It is intentionally not named
`production_certified`.

## Paired replay format

```json
{
  "cases": [
    {
      "case_id": "stable-case-id",
      "baseline": {
        "passed": true,
        "judge_score": 9,
        "duration_seconds": 10.2,
        "cost_usd": 0.10,
        "repair_attempts": 1
      },
      "candidate": {
        "passed": true,
        "judge_score": 10,
        "duration_seconds": 9.8,
        "cost_usd": 0.09,
        "repair_attempts": 0
      }
    }
  ]
}
```

Use identical immutable inputs for both sides. Missing cost or latency data is
a failed optimization gate, not zero. The comparison refuses promotion when a
baseline pass becomes a candidate failure even if aggregate averages improve.
Generate paired evidence with `scripts/run_replay_corpus.py`; it runs the
approval-negative and exact-plan positive path for every case on both versions,
then records run directories, pass/fail, judge score, duration, cost reported by
Pi, and repair attempts.

Comparison without `--promote` reports `eligible_not_promoted` when all gates
pass and never writes `active.json`. Only an invocation with `--promote` can
report `promoted` and `promotion_executed: true`.

## Specialized runtime boundary

The factory validates specialized entrypoint presence but does not invent a
production data adapter or a truthful domain verifier. Codex may create these
components from the blueprint, but the certification engine must execute their
domain fixtures before promotion. This is the intentional boundary between
reusable orchestration and workflow-specific truth.

The `specialized_fixture` dynamic gate runs that domain fixture through a
fixed convention: the compiled bundle must carry an executable
`scripts/certify.py`, invoked with no arguments and the harness root as its
working directory, that exits `0` on a passing fixture run and non-zero
otherwise. A specialized runtime without that adapter fails the gate
unconditionally.

## Interruption, cancellation, and resume

`run_pi_harness.py` already implements the process-group and single-workdir
half of this contract (`terminate_process_group`, `acquire_workspace_lock`,
`reconcile_stale_runs`, `finalize_interrupted_run`, `run_pi_checkpointed`,
`write_run_seal`). Any harness that also spawns containers, builds images, or
allocates scratch caches must extend the exact same registry/receipt/reap
pattern below rather than inventing a parallel mechanism — the SQLite ledger
and run seal must stay the single source of truth for "did this actually
finish or get torn down."

### Resource registry

Every external resource a run creates — a subprocess process group, a running
container, a built image, a scratch directory/volume, a sandbox VM — gets an
append-only receipt in `<run_dir>/resources/registry.jsonl` *before* it is
used: `{resource_id, kind, created_at, handle, reap_method, status: "active"}`.
Nothing is torn down by inference from stage output; only what is in the
registry is a live resource. Reap events append a new line with the same
`resource_id` and a terminal status — the registry is never rewritten in
place, matching the existing `events/harness.jsonl` append-only convention.

### Signal handling

- Install SIGINT/SIGTERM handlers before the first resource is created.
  SIGKILL is uncatchable and is the operator's own escalation path, not one
  the harness needs to detect.
- First signal: stop starting new stages, enter `canceling`, and reap every
  registry entry still `active`, **LIFO** — most-recently-created resource
  first, so a container using a scratch mount is stopped before that scratch
  path is deleted, and a subprocess reading a container's socket is killed
  before the container is stopped.
- Second signal (impatient operator, or teardown hanging): skip grace periods
  and go straight to hard-kill / force-remove for every remaining `active`
  entry, then exit. Still write the receipt with whatever reap results were
  achieved — a fast exit is not an excuse to skip the receipt.
- A hard external kill of the controller itself (SIGKILL, OOM killer, machine
  crash) leaves the run `status: running` with no receipt at all. This is what
  `reconcile_stale_runs` exists to detect and repair on the *next* invocation,
  not something the current process can protect against.

### Process groups

Generalizes the existing `terminate_process_group`: every subprocess launches
detached (`start_new_session=True` / `setsid`) so it owns its own group and one
`killpg` reaps its whole subtree, including anything a tool call itself forked.
Reap sequence: SIGTERM to the group → wait up to `grace_seconds`, polling
liveness → SIGKILL to the group if anything remains → re-poll every recorded
pid with `os.kill(pid, 0)`. Only mark `reaped` if the poll confirms nothing is
alive; a pid that survives SIGKILL (rare, e.g. uninterruptible I/O) is written
as `reap_failed` with the surviving pid, never silently upgraded to success.

### Containers

- Record `{container_id, image_ref, mounts, created_at}` before the run
  proceeds past the stage that started it.
- Reap order: SIGTERM the container's PID 1 → grace → SIGKILL/force-stop →
  **remove** the container (stopped-but-not-removed is not `reaped`) → verify
  removal with an inspect call that must return not-found.
- Runtime auto-remove (`--rm`-equivalent) is preferred as a crash-safety
  backstop but is not sufficient alone: it removes the container, not any
  run-specific image built only for this run (see Images below).
- If the container runtime is unreachable during teardown (daemon restarted,
  socket gone), record `reap_failed` with the error. Never swallow this into a
  clean `canceled` — a stuck container is an operator-visible fact, not a
  detail to hide behind a tidy cancellation.

### Images

- Only remove images the run itself built, tagged deterministically as
  `<workflow>-<run_id>[-<qualifier>]`. Never remove by wildcard or shared
  prefix across runs, and never remove a base/shared image another run or the
  operator's normal workflow depends on.
- Reap only after every container built from that image is confirmed reaped
  (registry order enforces this) — check "no other active registry entry
  references this image" before deleting even when order is respected, and
  record a `skipped:<reason>` status rather than deleting speculatively.
- A run-scoped builder's intermediate build cache is torn down with the image;
  a shared builder's cache is never torn down by an individual run.

### Scratch caches

- Every scratch directory/volume is created under a run-scoped path (default
  `<harness>/runs/<task_id>/<run_id>/scratch/`; an externally located path must
  still be recorded in the registry with its exact resolved path).
- Reap = recursive delete of exactly the recorded path, followed by an
  existence check to confirm it is gone. Never a glob or prefix cleanup that
  could reach a sibling run's scratch space — this is the same collision-safe,
  disjoint-roots discipline the skill already requires for immutable vs.
  writable paths, extended to writable scratch between concurrent runs.
- Ordering is a real dependency, not cosmetic: kill/stop every consumer
  process or container that might hold the cache open (their registry entries
  reaped first), *then* delete the cache. Deleting first risks a delete-then
  a still-running consumer recreating stale content, or the delete itself
  failing against an open handle and being wrongly retried as if idempotent.

### Concurrent SQLite-backed runs

- The per-workflow ledger (`<harness>/runs/harness.sqlite3`) is shared by every
  concurrent run of that workflow. Serialize only the ledger write itself with
  `registry_lock` (flock over a `.lock` sidecar) — acquire it for the single
  `sync_registry` call, never across the (possibly slow) resource-teardown
  phase. N runs canceling at once must not serialize on each other's container
  or image reaping, only on the few-millisecond row write.
- Two runs of the same `task_id`/workdir must not both mutate it:
  `acquire_workspace_lock`'s flock over the workdir hash already rejects a
  second concurrent run with a clear `SystemExit`. Extend the same lock scope
  to any shared external scratch/build-cache path a run points at outside its
  own run directory — two runs pointed at the same external scratch path must
  fail closed, not corrupt each other's cache.
- SQLite connections use `timeout=30` plus the flock for single-writer
  discipline; the flock is the actual serialization mechanism, SQLite's own
  busy-timeout is only a backstop.
- On interruption, the ledger row must never imply success. `canceled` is
  written and synced immediately using best-effort resource-reap state
  gathered so far — the row transition is not gated on teardown finishing.

### Cancellation / teardown receipt

Every teardown path — clean completion or interruption — writes
`<run_dir>/integrity/cancellation-receipt.json` enumerating every registry
resource, its final status (`reaped` / `reap_failed` / `skipped:<reason>`),
the signal path taken (`none` / `single_signal` / `double_signal`), and total
teardown duration. This is the concrete artifact behind "record every reaped
resource in the cancellation receipt."

### Resume semantics

- Resume is explicit (`--resume-run <dir>`) — a bare re-run of the same
  `task_id` starts a new run, it does not resume one.
- Resume is refused (fail closed) unless the spec digest, immutable-input
  digest, and harness implementation digest in `manifest.json` all still
  match current inputs. Any drift refuses resume outright.
- A per-stage checkpoint is reused only when its status is `verified`, its
  digest matches, and the on-disk stage artifact's sha256 still matches the
  checkpoint's recorded hash. Any mismatch forces that stage to re-run.
- Resume never reuses the prior attempt's resource-registry entries — every
  resumed run starts a fresh registry and re-provisions whatever process
  groups, containers, or scratch the stages that must re-run actually need.
  The prior attempt's resources are assumed torn down, or are reconciled as
  orphans (next bullet) before resume is allowed to proceed.
- Before a resume acquires the workspace lock, run stale-run reconciliation
  (`reconcile_stale_runs`): any run directory still marked `running` whose
  recorded owning pid/host is no longer alive is force-transitioned to
  `abandoned`. Extend reconciliation to also attempt best-effort reap of any
  registry entries still `active` for that abandoned run — a controller that
  died before its own teardown ran must not leave a container or scratch cache
  alive forever just because no process is left to notice.

### Certification additions for container/image/scratch capable harnesses

- kill -9 the controller mid-container-run → next invocation's stale-run
  reconciliation reaps the orphaned container/image/scratch and marks the run
  `abandoned`; no container is left running.
- concurrent cancel of two runs sharing one workflow ledger → both rows land
  `canceled`, neither corrupts the other's row, no deadlock on the registry
  lock.
- double-SIGINT → immediate hard-kill path exercised, receipt still written,
  no registry entry left `active` and unreported.
- scratch-cache deletion race → the case must fail if cache deletion is
  attempted while a consumer's registry entry is still `active`.
