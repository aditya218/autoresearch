# 02 — The Ledger: Event Log and Projections

The research ledger is an **append-only event log in Postgres**. Every entity table in
`01-data-model.md` is a *projection* over that log — derived, disposable, rebuildable.

Why event-sourced rather than mutable rows:

- **Recovery is replay.** A resuming run does not guess what happened; it reads what happened.
- **Audit.** "Why did the campaign try that at 3am?" is answerable, including the exact proposer
  context that produced the idea.
- **Time travel.** Reconstruct the ledger as of experiment 40 to reproduce a proposer decision.
- **Concurrency safety.** Per-stream sequence numbers give optimistic concurrency for free.

---

## Log schema

```sql
CREATE TABLE ledger_event (
  event_id        bigserial PRIMARY KEY,        -- global order, gapless per-partition
  campaign_id     uuid        NOT NULL,
  stream_type     text        NOT NULL,         -- 'campaign'|'hypothesis'|'experiment'|
                                                -- 'replicate'|'stage_execution'|'run'
  stream_id       uuid        NOT NULL,         -- entity this event belongs to
  stream_seq      int         NOT NULL,         -- 1..N within the stream
  event_type      text        NOT NULL,         -- see catalog below
  payload         jsonb       NOT NULL,
  run_id          uuid,                         -- writer
  fencing_token   bigint,                       -- writer's lease token; NULL for system events
  idempotency_key text,                         -- set for events representing side effects
  caused_by       bigint,                       -- event_id that triggered this one
  occurred_at     timestamptz NOT NULL DEFAULT now(),

  UNIQUE (stream_id, stream_seq),
  UNIQUE (idempotency_key)
);

CREATE INDEX ON ledger_event (campaign_id, event_id);
CREATE INDEX ON ledger_event (stream_type, stream_id, stream_seq);
```

### Append rules

1. **Fencing check.** An append with `fencing_token < campaign_lease.fencing_token` is rejected.
   Enforced in the append function, not in application code:

```sql
CREATE FUNCTION append_event(...) RETURNS bigint AS $$
BEGIN
  PERFORM 1 FROM campaign_lease
   WHERE campaign_id = p_campaign_id AND fencing_token = p_fencing_token
   FOR SHARE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'stale_fence' USING ERRCODE = 'P0001';
  END IF;
  -- insert with stream_seq = coalesce(max(stream_seq),0)+1, relying on the
  -- UNIQUE(stream_id, stream_seq) constraint to reject concurrent writers.
END $$ LANGUAGE plpgsql;
```

2. **Expected-sequence writes.** Callers that read-then-write pass the `stream_seq` they expect
   to produce. A unique violation means someone else moved the stream; the caller re-reads and
   retries. No advisory locks needed on the hot path.

3. **Events are immutable.** No updates, no deletes. A wrong event is corrected by a compensating
   event (`ExperimentInvalidated`), never by rewriting history.

4. **Idempotent append.** Re-appending with an existing `idempotency_key` is a no-op that returns
   the original `event_id`. This is what makes the whole recovery story work: a controller that
   crashed *after* the side effect but *before* recording it will, on retry, either write the
   event or discover it was already written — never both, never neither.

---

## Event catalog

### Campaign / run

| Event | Payload highlights |
| --- | --- |
| `CampaignCreated` | project_id, config, config_hash, parent_campaign_id |
| `CampaignStarted` | |
| `CampaignPaused` / `CampaignResumed` | actor, reason |
| `CampaignBudgetAdjusted` | field, old, new, actor — the only sanctioned config mutation |
| `CampaignStopped` | stop_reason, final_leaderboard_snapshot |
| `RunStarted` | worker_identity, engine_version, fencing_token |
| `RunHeartbeat` | *(not logged — heartbeats hit `campaign_lease` only; too high-volume)* |
| `RunEnded` | end_reason |
| `LeaseSeized` | previous_run_id, previous_token, new_token, staleness_seconds |

### Hypothesis

| Event | Payload highlights |
| --- | --- |
| `HypothesisProposed` | statement, rationale, parameters, predicted_effect, derived_from, **proposer_context_ref** |
| `HypothesisQueued` | priority |
| `HypothesisRejected` | reason (`duplicate` \| `out_of_budget` \| `policy` \| `unsafe` \| `low_value`), duplicate_of |
| `HypothesisClaimed` | run_id, lease_expires_at |
| `HypothesisReleased` | reason (`lease_expired` \| `run_drained`) |
| `HypothesisSuperseded` | superseded_by, reason |
| `HypothesisExpired` | staleness_metric |
| `HypothesisMaterialized` | experiment_ids[] |

`proposer_context_ref` is a content-addressed pointer to the *exact* prompt/context the proposer
saw. Without it, proposer behaviour is unreproducible and undebuggable.

### Experiment

| Event | Payload highlights |
| --- | --- |
| `ExperimentCreated` | hypothesis_id, role, resolved_config, resolved_config_hash, provenance, workflow_version |
| `ExperimentAdmitted` | slot, budget_reserved |
| `ExperimentResultReused` | source_experiment_id, reason — cache hit, no execution |
| `ExperimentStarted` | |
| `ReplicateStarted` / `ReplicateFinished` | seed, outcome, metrics, cost |
| `ExperimentMetricsRecorded` | aggregated metrics, dispersion, guardrail evaluation |
| `ExperimentSucceeded` | metrics, cost, artifacts |
| `ExperimentFailed` | failure_class (`experiment` \| `infra`), error, retryable |
| `ExperimentAborted` | actor, reason (budget, manual, campaign stop) |
| `ExperimentInvalidated` | reason (`contamination` \| `provenance_drift` \| `metric_bug`), actor |
| `ExperimentAnalyzed` | analysis text, extracted findings |

`ExperimentInvalidated` is the compensating event. Results proven bogus after the fact are
retracted, not deleted — and the leaderboard projection excludes them while the log keeps them.

### Stage execution

| Event | Payload highlights |
| --- | --- |
| `StageLaunchIntent` | stage_key, attempt, kind, idempotency_key, planned_inputs_hash |
| `StageLaunched` | external_handle |
| `StageReattached` | external_handle, discovered_via (`log` \| `external_scan`) |
| `StageProgress` | *(sampled — see rate limits)* |
| `StageCompleted` | outputs, cost |
| `StageFailed` | failure_class, error, attempt, will_retry |
| `StageCancelled` | reason |
| `StageOrphanDetected` | idempotency_key, external_handle, action (`adopted` \| `killed`) |

`StageLaunchIntent` **must be committed before the external launch call is made.** See
`04-durability.md` — this ordering is the entire basis of the "no double-launch, no lost work"
guarantee.

### Human / governance

| Event | Payload highlights |
| --- | --- |
| `ApprovalRequested` | gate, subject, cost_estimate |
| `ApprovalGranted` / `ApprovalDenied` | actor, note |
| `HumanNoteAdded` | text, attached_to — human guidance the proposer must read |
| `KillSwitchEngaged` | actor, scope (campaign \| project \| global) |

---

## Projections

Projections consume the log in `event_id` order and write the tables in `01-data-model.md`.

```sql
CREATE TABLE projection_offset (
  projection_name text PRIMARY KEY,
  last_event_id   bigint NOT NULL,
  updated_at      timestamptz NOT NULL DEFAULT now()
);
```

Rules:

- **Synchronous by default.** The projection update runs in the *same transaction* as the append
  for the entity tables. This keeps read-your-writes semantics inside the controller and removes
  a class of "the controller acted on stale state" bugs. Postgres makes this cheap; revisit only
  if append throughput becomes a real problem.
- **Asynchronous for derived analytics** — leaderboards, research summaries, embeddings,
  cross-campaign rollups. These tolerate lag and are rebuilt from scratch on schema change.
- **Rebuildable.** `rebuild_projection(name)` truncates and replays from `event_id = 0`. There
  must be a test asserting that a full rebuild reproduces the live tables byte-for-byte; this is
  the property that keeps the log authoritative rather than decorative.

### Core projections

| Projection | Purpose |
| --- | --- |
| `entity_state` | The tables in doc 01 |
| `leaderboard` | Ranked experiments per campaign under the objective, excluding invalidated |
| `idea_queue` | Claimable hypotheses, ordered by priority, with lease state |
| `budget_ledger` | Running cost and reservation totals per campaign |
| `research_summary` | Incrementally-maintained digest fed to the proposer (doc 05) |
| `registry` | Cross-project/campaign/run listing (doc 01) |

### Retention

The log is the permanent record; artifacts are not. Events are retained indefinitely
(they are small). Large artifacts live in content-addressed object storage with a per-project
retention policy, and the log stores only refs. A garbage-collected artifact leaves the log
valid with a dangling-but-labelled ref rather than a broken projection.
