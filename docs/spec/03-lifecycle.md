# 03 — Lifecycles and State Machines

Every state transition is an event (`02-event-log.md`). Illegal transitions are rejected by the
append function, not merely avoided by application code.

---

## Campaign

```
DRAFT ──start──> ACTIVE ⇄ PAUSED
                   │        │
                   ├────────┴──stop──> STOPPING ──> COMPLETED ──archive──> ARCHIVED
```

| State | Meaning |
| --- | --- |
| `DRAFT` | Config mutable. No runs, no experiments. |
| `ACTIVE` | Config frozen. Runs may acquire the lease and drive work. |
| `PAUSED` | No new hypotheses proposed, no new experiments admitted. **In-flight experiments continue** — killing paid-for work to honour a pause is waste. |
| `STOPPING` | Draining. In-flight experiments **run to completion** — there is no cancel in v1 (D24). The controller stays alive to collect their results. |
| `COMPLETED` | Terminal. `stop_reason` set. Leaderboard snapshotted into `CampaignStopped`. |
| `ARCHIVED` | Terminal + cold. Artifacts eligible for GC; log retained. |

`stop_reason` ∈ `budget_exhausted` | `converged` | `target_reached` | `manual` | `fatal_error`.

**Every stop path gates admission, not execution** (D24). Pause, stop, budget exhaustion, the
circuit breaker, and the kill switch all stop *new* work; none of them stop work already running.
Without a cancel command there is no mechanism that could, and pretending otherwise would be worse
than stating it.

The operational consequence is counterintuitive: **stopping a campaign requires the controller to
stay alive longer, not exit sooner.** In-flight experiments represent hours of compute already
paid for, and abandoning the controller throws away results that are minutes from being recorded.
`STOPPING` therefore drains — polling to completion, recording metrics — and only then reaches
`COMPLETED`.

---

## Run

```
       acquire lease
  ────────────────────> ACTIVE ──drain──> DRAINING ──> ENDED
                          │                              ▲
                          └────── lease lost / crash ─────┘
```

- **ACTIVE** — holds the lease, heartbeating. Only an ACTIVE run with the current fencing token
  may append campaign-mutating events.
- **DRAINING** — stops claiming new work, releases hypothesis claims, lets in-flight stage polls
  finish, then releases the lease cleanly. External jobs are **not** killed; they are left for
  the next run to re-attach to.
- **ENDED** — terminal. `end_reason` ∈ `clean_shutdown` | `lease_lost` | `crashed` | `campaign_stopped`.

A run that discovers its fencing token is stale (append rejected with `stale_fence`) must
immediately stop all work and transition to `ENDED(lease_lost)`. It must not attempt to reacquire
within the same process lifetime — another run is authoritative and re-entry invites split brain.

---

## Hypothesis

```
                            ┌──────────────> REJECTED (terminal)
                            │
PROPOSED ──admit──> QUEUED ─┼──claim──> CLAIMED ──materialize──> MATERIALIZED (terminal)
                       ▲    │              │
                       │    └──────────> EXPIRED (terminal)
                       └──release────────┘
                            │
                            └──────────> SUPERSEDED (terminal)
```

| Transition | Trigger |
| --- | --- |
| `PROPOSED → QUEUED` | Passes admission control: dedup, safety policy, budget feasibility |
| `PROPOSED → REJECTED` | `duplicate` \| `unsafe` \| `policy` \| `out_of_budget` \| `low_value` |
| `QUEUED → CLAIMED` | A run atomically claims it with a lease (`claim.expires_at`) |
| `CLAIMED → QUEUED` | Claim lease expired, or the run drained. **Requeue, never drop.** |
| `CLAIMED → MATERIALIZED` | One or more experiments created from it |
| `QUEUED → EXPIRED` | Staleness policy fired (doc 05) |
| `QUEUED → SUPERSEDED` | A later hypothesis subsumes it; `superseded_by` recorded |

`MATERIALIZED` is terminal for the *hypothesis*, even if its experiments later fail. The idea was
tried; what happened next is the experiment's story. A hypothesis worth retrying produces a new
hypothesis with `derived_from` pointing at the original — this keeps "how many distinct ideas
have we tried" a meaningful number.

### Claiming (the concurrency-safe primitive)

```sql
UPDATE hypothesis SET state = 'CLAIMED',
       claim = jsonb_build_object('run_id', $1, 'expires_at', now() + interval '5 minutes')
 WHERE hypothesis_id = (
   SELECT hypothesis_id FROM hypothesis
    WHERE campaign_id = $2 AND state = 'QUEUED'
    ORDER BY priority DESC, created_at
    FOR UPDATE SKIP LOCKED LIMIT 1)
RETURNING *;
```

`FOR UPDATE SKIP LOCKED` gives correct multi-worker claiming with no distributed lock. The claim
lease is short because materialization is fast; the *experiment* carries its own durability.

---

## Experiment

```
CREATED ──admit──> ADMITTED ──> RUNNING ──> AGGREGATING ──> SUCCEEDED
   │                  │            │                │
   │                  │            │                └─────> FAILED
   │                  │            └──────────────────────> FAILED
   │                  └───────────────────────────────────> ABORTED
   └──cache hit──────────────────────────────────────────> SUCCEEDED (reused)

  any terminal state ──retract──> INVALIDATED
```

| State | Meaning |
| --- | --- |
| `CREATED` | Materialized from a hypothesis; config resolved; not yet holding a slot |
| `ADMITTED` | Holds a concurrency slot and a budget reservation |
| `RUNNING` | ≥1 replicate executing the workflow. The `implement` and `review` stages happen here — an experiment has no commit until `implement` completes, and a `review` rejection ends it as `experiment_failure` before any job is launched |
| `AGGREGATING` | All replicates terminal; computing metrics, guardrails, analysis |
| `SUCCEEDED` | Workflow completed, metrics recorded, guardrails evaluated |
| `FAILED` | See failure taxonomy below |
| `ABORTED` | Never entered before an external job launches in v1 (D24); reachable only pre-launch — diff review rejection, admission withdrawal, or campaign kill before the `train` stage starts |
| `INVALIDATED` | Retracted after the fact. Excluded from the leaderboard, retained in the log |

### Failure taxonomy

This distinction is load-bearing — it determines what the proposer learns and what the budget
is charged. Collapsing these three into one status is the mistake that makes autonomous loops
draw wrong conclusions and burn money on doomed retries.

| Class | Example | Retried? | Charged to budget? | Shown to proposer? |
| --- | --- | --- | --- | --- |
| `infra_failure` | OOM-killed node, image pull error, cloud quota, network | **Yes**, with backoff, capped attempts | Cost incurred is charged; the experiment slot is not consumed | **No** — it is not a research result |
| `experiment_failure` | Generated code doesn't compile; diff review rejects it; training diverges; eval crashes | No (a retry would fail identically) | Yes | **Yes** — a real, informative negative result. "You may not modify the benchmark" is exactly the feedback that improves the next proposal |
| `aborted` | Budget hit mid-flight; operator kill | No | Partial cost charged | As "not evaluated", never as a negative result |

Ambiguous cases resolve to `infra_failure` **at most N times** (default 3), after which they are
reclassified `experiment_failure`. Otherwise a genuinely broken idea retries forever.

### Guardrails

An experiment that produces metrics but violates a guardrail is `SUCCEEDED` with
`guardrail_violations` populated — it ran correctly and produced a valid, informative result.
It is simply excluded from the leaderboard by the objective's feasibility filter. Marking it
FAILED would hide a real finding from the proposer.

---

## Replicate

```
PENDING ──> RUNNING ──> COMPLETED
                 └────> FAILED (class: infra | experiment)
                 └────> CANCELLED
```

If a replicate fails on infra, only that replicate retries. If replicates disagree beyond the
objective's tolerance, the experiment is flagged `high_variance` in `ExperimentMetricsRecorded` —
a signal both to the proposer and to the confirmation policy (doc 07).

---

## Stage execution

The inner loop. Both stage kinds share one state machine; only the transition mechanics differ.

```
PENDING ──intent──> LAUNCH_INTENT ──launch──> LAUNCHED ──observe──> RUNNING ──> COMPLETED
                          │                       ▲                     │
                          │                       └──reattach───────────┤
                          │                                             ├──> FAILED
                          └──recover: scan external system for key ─────┤
                                                                        └──> CANCELLED
```

| State | Meaning |
| --- | --- |
| `PENDING` | Dependencies satisfied, not yet started |
| `LAUNCH_INTENT` | Intent committed with an idempotency key; **the side effect may or may not have happened.** The only genuinely ambiguous state, and recovery resolves it by scanning the external system for the key |
| `LAUNCHED` | External handle recorded (or in-process task started) |
| `RUNNING` | Confirmed executing |
| `COMPLETED` | Outputs written to the stage directory and recorded in the ledger |
| `FAILED` | With failure class; retry creates a **new** stage_execution row with `attempt + 1`, never mutates this one |
| `CANCELLED` | Experiment aborted or campaign killed |

For `local` stages, `LAUNCH_INTENT → LAUNCHED` is instantaneous and recovery from
`LAUNCH_INTENT` or `RUNNING` means **re-execute from the start of the stage** — which is exactly
why expensive work must not be an in-process stage. For `external_job` stages, recovery means
**re-attach**, and no work is lost. See `04-durability.md`.
