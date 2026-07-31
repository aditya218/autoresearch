# 02 — State, Transitions, and the Debug Log

**The state tables in `01-data-model.md` are the source of truth.** Entity state lives in mutable
columns — `experiment.state`, `experiment.current_stage`, `stage_execution.state` — updated in
place.

Alongside them is an append-only `transition_log`, written in the same transaction as every state
change. It exists **for humans debugging a campaign**, and for nothing else.

This is deliberately not event sourcing. The tables are not projections and there is no replay
path. See §6 for what that costs.

---

## 1. Why the state table is enough

The durability guarantees in `04-durability.md` do not depend on an event log. Each rests on
something simpler:

| Guarantee | Mechanism | Needs events? |
| --- | --- | --- |
| No double-launch | `UNIQUE (idempotency_key)` on `stage_execution` | No |
| No lost in-flight job | Intent row committed before `launch.sh` runs | No |
| No zombie writer | Fencing token checked in the `WHERE` clause | No |
| Correct resume point | `SELECT … WHERE state IN (non-terminal)` | No |
| Concurrency safety | Compare-and-swap on `state` | No |

And history — the thing an event log is genuinely for — is largely free here anyway: one row per
experiment and one row per stage attempt, retained forever. Those tables are append-only by
nature. The proposer's context is a `SELECT` over experiment rows, not a replay.

What is *not* free is the **transition history**: when a state changed, why, which run did it, and
what the engine was thinking at the time. That is what the log below captures, and it is the whole
of its job.

---

## 2. State transitions

Every transition is a compare-and-swap that also checks the writer's lease. A single helper
performs the update and the log write together, so they cannot diverge:

```sql
CREATE FUNCTION transition(
  p_entity_type  text,   p_entity_id  uuid,
  p_from         text,   p_to         text,
  p_reason       text,   p_detail     jsonb,
  p_run_id       uuid,   p_fencing_token bigint
) RETURNS boolean AS $$
DECLARE updated int;
BEGIN
  -- fencing: a superseded run cannot mutate the campaign (doc 04 §1)
  PERFORM 1 FROM campaign_lease
   WHERE campaign_id = campaign_of(p_entity_type, p_entity_id)
     AND fencing_token = p_fencing_token;
  IF NOT FOUND THEN RAISE EXCEPTION 'stale_fence'; END IF;

  EXECUTE format('UPDATE %I SET state = $1, updated_at = now()
                   WHERE %I = $2 AND state = $3', ...)
    USING p_to, p_entity_id, p_from;
  GET DIAGNOSTICS updated = ROW_COUNT;

  IF updated = 0 THEN RETURN false; END IF;   -- someone else moved it; caller re-reads

  INSERT INTO transition_log(entity_type, entity_id, from_state, to_state,
                             reason, detail, run_id, occurred_at)
  VALUES (p_entity_type, p_entity_id, p_from, p_to, p_reason, p_detail, p_run_id, now());

  RETURN true;
END $$ LANGUAGE plpgsql;
```

Three properties worth stating explicitly:

- **CAS, not blind update.** `WHERE state = $from` with a row-count check. A zero row count means
  another writer moved the entity; the caller re-reads and decides. This is what replaces the
  per-stream sequence numbers an event log would have given.
- **Fencing lives in the database.** Application code cannot forget it. A run whose lease was
  seized gets `stale_fence` on its next write regardless of what it believes (doc 04 §1).
- **Illegal transitions are rejected**, not merely avoided. The legal transition table from
  `03-lifecycle.md` is enforced here, so a state machine bug surfaces as an error rather than as a
  campaign in an impossible state at 3am.

---

## 3. The transition log

```sql
CREATE TABLE transition_log (
  id           bigserial PRIMARY KEY,
  campaign_id  uuid        NOT NULL,
  entity_type  text        NOT NULL,   -- 'campaign'|'hypothesis'|'experiment'|
                                       -- 'replicate'|'stage_execution'|'run'
  entity_id    uuid        NOT NULL,
  from_state   text,
  to_state     text,                   -- NULL for decision records (§4)
  reason       text        NOT NULL,   -- short code: 'infra_failure', 'lease_expired', …
  detail       jsonb,                  -- whatever a human would want at 3am
  run_id       uuid,
  actor        text,                   -- set for human-originated changes (D20)
  occurred_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ON transition_log (campaign_id, occurred_at);
CREATE INDEX ON transition_log (entity_type, entity_id, occurred_at);
```

### The two rules that keep it honest

**Rule 1 — same transaction, always.** The log write commits with the state change or neither
happens. Not an async write, not a logging framework, not best-effort.

A best-effort debug log drifts from the state table, and it drifts *precisely* under the
conditions you most want it for — crashes, contention, partial failures. A log that is trustworthy
except during incidents is a log that is not trustworthy. The cost of this rule is one INSERT per
transition, which at D17 scale is nothing.

**Rule 2 — nothing in the control path reads it.** Only humans, the CLI, and the inspector query
`transition_log`. No scheduling decision, no recovery step, no proposer input touches it.

This is the rule that stops the design sliding back into event sourcing. The moment the control
loop reads the log to decide something, the log becomes load-bearing — and you have re-acquired
every cost of event sourcing (correctness of replay, consistency with state, a rebuild test)
without any of the discipline that would have made those costs safe. Enforceable as a real
constraint: **no module under `control/` may import the transition-log reader.** Worth a lint
check, because the temptation shows up as something innocuous like "just count how many times this
retried."

If the control loop needs a fact, that fact belongs in a state column. `infra_attempt_count`
on `stage_execution` is a column, not something derived by counting log rows.

---

## 4. What to record beyond state changes

The state table already says *what* an entity is. The log's value is *why*, so it also records
decisions that are not themselves transitions:

| Record | Detail worth capturing |
| --- | --- |
| `hypothesis_rejected` | Which admission check failed, and against which prior hypothesis |
| `experiment_cache_hit` | The source experiment and the matching `diff_hash` |
| `triage_verdict` | Class, confidence, and the verifying log excerpt (doc 06) |
| `budget_check` | Remaining budget and the reservation computed at admission |
| `proposal_made` | `proposer_context_ref`, and how many candidates were emitted vs. admitted |
| `lease_seized` | Previous run and token, observed staleness |
| `orphan_detected` | Job id, its idempotency key, what the reaper decided |
| `provenance_mismatch` | Intended vs. actual commit reported by the job system |
| `summary_drift` | Divergence found by the periodic re-derivation audit (doc 05) |

These carry `to_state = NULL`. They are the entries that answer the questions a campaign actually
raises: not "what state is this in" — the table says that — but "why did it choose this, and what
did it know when it did."

### What not to record

- **Polls.** Every tick polls every in-flight stage. Only transitions get logged; `last_polled_at`
  is a column updated in place.
- **Heartbeats.** They touch `campaign_lease` only.
- **Intermediate metrics.** High volume; they belong in artifact files, with the log carrying only
  a reference.

---

## 5. Records that are genuinely records

A few things are not state and not transitions — they are facts that happened, and they live in
their own tables rather than as columns:

| Table | Contents |
| --- | --- |
| `human_note` | Steering notes for the proposer (D14). Timestamped, attributed, never deleted |
| `approval` | Requests, grants, denials, with actor and note |
| `campaign_amendment` | In-place config edits permitted by D18: field, old, new, actor |
| `proposer_context` | The exact brief given to the proposer, referenced by `proposer_context_ref` |

Unlike `transition_log`, **the control path may read these** — steering notes go into every
proposer brief, approvals gate admission. They are inputs to the system, not a record of it.

---

## 6. What this design gives up

Stated plainly, so nobody discovers it during an incident:

- **No point-in-time reconstruction.** You cannot ask "show me the ledger exactly as of experiment
  40." You can read what changed and when, but not cheaply rebuild the whole world at that moment.
  In practice this is a debugging luxury; immutable experiment rows plus the transition log cover
  the realistic version.
- **No replay of proposer decisions.** Mitigated by storing `proposer_context` verbatim — the
  input to any past decision is retained even though the surrounding state is not reconstructible.
- **Corrections overwrite.** A wrong state is fixed by transitioning out of it, and the transition
  log records that it happened. Retraction of a *result* is different and must not overwrite: it
  sets `invalidated_at` and a reason, leaving metrics intact, because doc 07's confirmation policy
  depends on retracted results staying visible.

---

## 7. Immutability where it still matters

Mutable state does not mean mutable history. Enforced by trigger:

1. **`transition_log` is INSERT-only.** No updates, no deletes.
2. **Terminal experiment rows are frozen.** Once `SUCCEEDED` or `FAILED`, only `invalidated_at`,
   `invalidation_reason`, and `analysis` may change. Metrics and provenance never change.
3. **`stage_execution` rows are never reused.** A retry INSERTs `attempt + 1`; it never mutates the
   failed attempt.
4. **Hypotheses are never deleted.** Rejection and expiry are states, so "what did we consider and
   decline" stays answerable — which the proposer depends on to stop re-proposing rejected ideas.
