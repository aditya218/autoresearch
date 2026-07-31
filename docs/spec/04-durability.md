# 04 — Durability, Leases, and Recovery

**Requirement:** a campaign survives a crash of any component at any instant, and resumes with
minimal to no wasted work.

Two independent mechanisms deliver this:

1. **Lease + fencing** — exactly one run drives a campaign at a time, and a zombie run cannot
   corrupt the ledger after it has been superseded.
2. **Intent-before-effect + re-attach** — no external side effect happens without a durably
   recorded, keyed intent, and expensive in-flight work is rejoined rather than relaunched.

---

## 1. Campaign lease

```sql
CREATE TABLE campaign_lease (
  campaign_id   uuid PRIMARY KEY,
  run_id        uuid        NOT NULL,
  fencing_token bigint      NOT NULL,        -- strictly monotonic per campaign
  expires_at    timestamptz NOT NULL,
  heartbeat_at  timestamptz NOT NULL
);
```

**Acquire** (atomic; succeeds only if the lease is free or expired):

```sql
INSERT INTO campaign_lease AS l (campaign_id, run_id, fencing_token, expires_at, heartbeat_at)
VALUES ($campaign, $run, 1, now() + $ttl, now())
ON CONFLICT (campaign_id) DO UPDATE
   SET run_id = EXCLUDED.run_id,
       fencing_token = l.fencing_token + 1,
       expires_at = EXCLUDED.expires_at,
       heartbeat_at = now()
 WHERE l.expires_at < now()
RETURNING fencing_token;
```

No rows returned ⇒ another run holds a live lease ⇒ this process exits (or waits, per config).

**Renew** every `ttl/3`; **TTL** default 60s. A run must treat a failed renewal as immediate loss
of authority: stop issuing side effects *before* the TTL expires, not after — otherwise there is
a window where two processes both believe they hold the lease. Concretely, a run stops launching
new work once `now() > heartbeat_at + ttl/2` and has not successfully renewed.

**Fencing** is what makes this safe rather than merely usually-safe. Clock skew, a 90-second GC
pause, or a paused VM can all make a run believe it still holds an expired lease. The monotonic
token is checked inside `append_event` (`02-event-log.md`), so a zombie's writes are rejected by
the database regardless of what the zombie believes. **Every external side effect must also carry
the token** where the target system supports it (job labels, object-storage preconditions) so
zombie launches are attributable and reapable.

**Seizure** is logged as `LeaseSeized` with the previous token and observed staleness — the
primary forensic signal for "why did two things happen at once".

---

## 2. Intent before effect

The invariant, for any action outside the database:

> Commit the intent, with a key, before performing the effect. Stamp the key onto the effect.
> On recovery, resolve ambiguity by looking the key up in the external system.

```
1. append StageLaunchIntent{ idempotency_key = K }              [committed]
2. AUTORESEARCH_IDEM_KEY=K ./scripts/launch.sh ... -> job_id     [ambiguous window]
3. append StageLaunched{ job_id }                                [committed]
```

A crash between 1 and 3 leaves the stage in `LAUNCH_INTENT` — the one ambiguous state, and with
1–8 hour jobs (D7) an unresolved one means a GPU job running with nobody watching it. Recovery
uses the `find` command (D11):

```
for each stage_execution in LAUNCH_INTENT:
    job_id = AUTORESEARCH_IDEM_KEY=K ./scripts/find.sh
    if job_id and poll(job_id) is alive:     append StageReattached{job_id}  -> LAUNCHED
    if job_id and poll(job_id) is terminal:  ingest outputs   -> COMPLETED | FAILED
    if no job_id:                            re-launch with the same K  -> exactly-once
```

### Idempotency key construction

```
K = ar-{campaign_id[:8]}-{experiment_id[:8]}-{stage_key}-{attempt}
```

Structured rather than hashed, deliberately. The requirement is only that the key be
**deterministic and derivable from state alone**, so a recovering run computes the identical key
without remembering anything — a hash was one way to achieve that, not the point. A structured
name is greppable in your existing job tooling, so an operator looking at a running job at 2am can
see which experiment it belongs to without consulting the ledger.

`attempt` makes an intentional retry a distinct effect. There is no `inputs_hash`: in this design
a change of inputs means a new commit, which means a new experiment, so the experiment id already
covers what the hash was guarding against.

The `ar-` prefix is load-bearing for orphan reaping below — it is what distinguishes engine jobs
from everything else running under the same account.

The engine passes `K` to `launch.sh` as `AUTORESEARCH_IDEM_KEY`, and **the launcher must tag the
submitted job with it** — a label, a job name, a comment field, whatever the scheduler supports —
so that `find.sh` can return it later (D11). This is the only requirement the engine places on
your infrastructure. A workflow declaring an `external_job` stage without a `find` command fails
spec validation, because without it exactly-once launch is unachievable and the durability
guarantee is a fiction.

### Orphan reaping

A background sweep lists running jobs for the engine's account and compares them against
non-terminal `stage_execution` rows. A running job with no live stage — the residue of a zombie run
or a botched recovery — is logged as `StageOrphanDetected` and adopted if it matches a stage
awaiting work.

**Two hard rules, both about not touching things that are not ours:**

1. **The sweep considers only jobs whose name carries the `ar-` prefix.** Job listings are scoped
   by username, and if the engine runs under a human's account that listing includes jobs they
   launched by hand. Everything without the prefix is invisible to the reaper, always. Running the
   engine under its own service account is strongly preferred, but the prefix guard must hold even
   when it does not.
2. **The reaper does not kill in v1** (D24). With no cancel command it can only adopt, or record
   the orphan and let it run to completion. This makes the sweep read-only, which removes the
   entire class of "the reaper killed the wrong job" failure — worth noting as a real consolation
   for not having cancel.

A leaked 8-hour GPU job is still the most expensive failure mode in the system, and in v1 the
mitigation is detection and alerting rather than termination.

---

## 3. What "minimal wasted work" actually means

| Failure | Cheap-path recovery | Work lost |
| --- | --- | --- |
| Controller crash, external stage running | Re-attach via label scan | **None** |
| Controller crash, local stage running | Re-execute the stage | That stage only (≤20 min by lint rule) |
| Controller crash between stages | Resume at next PENDING stage | None |
| External job killed (spot preemption) | Retry as `infra_failure`; resume from stage checkpoint if the stage supports one | Stage progress since last checkpoint |
| Postgres failover | Reconnect; uncommitted appends retried by key | None |
| Whole region gone | Replay log in new region; external handles dead; stages retried | All in-flight external work |

The design target is the first row. **The single most important rule that follows: expensive work
belongs in `external_job` stages.** A local stage is only appropriate for work cheap enough to
redo — writing the change, reviewing the diff, compiling, analysis. This should be enforced
by lint on the workflow spec: a `local` stage declaring an expected duration above a
threshold (default 20 minutes) is a spec error.

### Stage-level checkpointing (optional, per stage)

A stage may declare `checkpoint: { every: "10m", uri_template: ... }`. On retry, the stage
contract receives the last checkpoint ref. This turns spot-preemption loss from "the whole stage"
into "since the last checkpoint". Optional because it requires cooperation from the workload.

---

## 4. Startup recovery sequence

A run performs this deterministically on start, before doing any new work:

```
1.  Acquire lease → obtain fencing token. Abort if held.
2.  Append RunStarted.
3.  Load campaign config (immutable) and verify config_hash.
4.  Rebuild/verify projection freshness: projection_offset == max(event_id).
5.  RECONCILE STAGES:
      LAUNCH_INTENT  -> external scan by idempotency key (see above)
      LAUNCHED       -> poll handle; dead & not terminal -> infra_failure -> retry
      RUNNING        -> poll handle; resume polling loop
      local non-terminal      -> mark attempt failed(infra), schedule attempt+1
6.  RECONCILE EXPERIMENTS:
      all replicates terminal but experiment not -> resume at AGGREGATING
      ADMITTED with no replicates started        -> start them
      RUNNING with no live stages and no work    -> re-derive next stage from workflow DAG
7.  RECONCILE HYPOTHESES:
      CLAIMED with expired claim -> release to QUEUED
8.  REAP ORPHANS: external jobs labelled for this campaign with no matching stage.
9.  RECOMPUTE BUDGET: sum actual costs from log; release reservations for terminal experiments.
10. Resume the control loop.
```

Recovery is pure replay plus external reconciliation — the controller holds **no** in-memory state
that cannot be reconstructed from step 1–9. That property is a hard design constraint, and the
recovery sequence above is its test: anything a run needs but cannot rebuild here is a bug.

---

## 5. Control loop

Each iteration, under a live lease:

```
tick():
  if lease not healthy: drain and exit
  if campaign.status != ACTIVE: handle pause/stop and return
  poll_external_stages()          # advance in-flight work first — it is already paid for
  advance_ready_stages()          # start stages whose dependencies are satisfied
  finalize_completed_experiments()# aggregate, evaluate guardrails, analyze, record
  check_stopping_criteria()       # budget, convergence, target, kill switch
  admit_experiments()             # claim queued hypotheses -> experiments, up to slot limit
  maybe_propose()                 # if queue depth below watermark and budget allows
```

Ordering is deliberate: **advance work already in flight before creating more.** A controller that
proposes first will, under a crash loop, accumulate in-flight experiments it never advances.

Proposal is invoked when `queued_hypotheses < low_watermark` (default: `max_concurrent` slots
worth), not on a timer — the queue is the backpressure signal.

---

## 6. Failure-injection test suite (required, not optional)

Durability claims that are not continuously tested are aspirations. Minimum set:

| Test | Asserts |
| --- | --- |
| Kill controller at every event boundary of a full experiment | Replay reaches identical terminal state; no duplicate external launches |
| Kill between `StageLaunchIntent` and launch | Recovery launches exactly once |
| Kill between launch and `StageLaunched` | Recovery re-attaches, does not relaunch |
| Two runs race for the lease | Exactly one wins; loser's appends rejected with `stale_fence` |
| Zombie run (paused 5×TTL, then resumes) | All its appends rejected; its orphan jobs reaped |
| Full projection rebuild from event 0 | Byte-identical to live projection tables |
| External system returns success after client timeout | No double-charge, no double-launch |
| Postgres connection drops mid-append | Retry by key is a no-op or completes; never both |
