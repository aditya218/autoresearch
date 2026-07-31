# 09 — Interfaces and Implementation Layout

Per D21: **CLI plus a read-only web inspector**. Per D22: **Python, minimal dependencies** — the
engine owns its control loop rather than delegating to a workflow framework, because the
durability semantics in doc 04 are the product, not an implementation detail to outsource.

Per D20 the instance is shared by a small trusted team: every command records an actor, and no
command checks a permission.

---

## Why no workflow framework

Temporal or Prefect would supply durable execution, which is most of doc 04. It is still the wrong
choice here:

- The ledger is event-sourced in Postgres (D2) and is the product's core artifact — the thing the
  proposer reads and the thing a human audits. A framework's own event history would be a second,
  competing source of truth.
- The durability contract is specific: re-attach to an 8-hour job by scanning for an idempotency
  key (D11). That is a handful of well-understood mechanisms, not a framework's worth.
- It removes an operational dependency from a system that must run unattended for days.

The cost is that the failure-injection suite in doc 04 §6 is mandatory rather than optional. That
suite is how the durability claims stay true, and it would be worth writing even with a framework.

---

## Dependencies

| Need | Choice |
| --- | --- |
| Postgres driver | `psycopg[binary]` — raw SQL, no ORM. The schema is small and the queries are deliberate |
| Migrations | plain SQL files, applied in order |
| CLI | `typer` or `argparse` |
| Web inspector | `fastapi` + server-rendered templates. No frontend build step |
| LLM client | `anthropic` |
| Config | `pydantic` for workflow-spec and config validation — the lint rules in doc 06 need a real schema layer |
| Git | subprocess to `git`. No library |
| Sandboxing | subprocess with rlimits and a container runtime where available (doc 08) |

Deliberately absent: a workflow engine, an ORM, a task queue, an experiment tracker. The ledger
is the experiment tracker.

---

## Package layout

```
autoresearch/
  ledger/          append_event, projections, migrations, replay
  domain/          entities, state machines, transition validation
  control/         run loop, lease, recovery, admission, budget
  executors/       command executor, local sandboxed executor
  workflow/        spec parsing, lint rules, DAG resolution
  agents/          proposer, coding agent, analyst, summarizer
  safety/          diff review, protected paths, secret scan, sandbox policy
  objective/       metric registry, aggregation, comparison, confirmation
  cli/
  web/
  testing/         failure injection harness (doc 04 §6)
```

The dependency rule: `ledger` and `domain` depend on nothing else in the package; `control`
depends on both; `agents`, `executors`, and `safety` are called by `control` and never call it
back. A coding agent that could write to the ledger would be a coding agent that could report its
own results (doc 08 §5), so this is a safety boundary, not just tidiness.

---

## CLI

```
autoresearch project create --name v4-latency --metrics metrics.yaml
autoresearch project list

autoresearch campaign create --project <id> --config campaign.yaml   # validates, stays DRAFT
autoresearch campaign start   <id>                                   # freezes config, ACTIVE
autoresearch campaign pause   <id>                                   # gates admission, not execution
autoresearch campaign resume  <id>
autoresearch campaign stop    <id>       # drains; in-flight jobs run to completion (D24)
autoresearch campaign fork    <id> --edit objective.target=0.95      # for frozen fields (D18)
autoresearch campaign amend   <id> --set budget.max_cost_usd=8000    # for editable fields
autoresearch campaign status  <id>
autoresearch campaign report  <id>                                   # winner + branch (D19)

autoresearch run start   --campaign <id>     # acquires lease, drives the loop; exits if held
autoresearch run drain   --campaign <id>

autoresearch idea add    --campaign <id> --file idea.md    # origin: human (D14)
autoresearch note add    --campaign <id> "stop trying memory-layout changes"
autoresearch approve     <request_id> [--deny]

autoresearch exp list    --campaign <id> [--state RUNNING]
autoresearch exp show    <id>            # metrics, stages, cost, lineage
autoresearch exp diff    <id>            # the agent's change
autoresearch exp logs    <id> [--stage train]
autoresearch exp invalidate <id> --reason contamination

autoresearch kill        --campaign <id>  # halt admission now; in-flight jobs still complete
autoresearch ledger replay --campaign <id> --as-of <event_id>
autoresearch ledger verify --campaign <id>   # rebuild projections, diff against live
```

`run start` is deliberately separate from `campaign start`. A campaign is a durable object that
exists whether or not anything is driving it; a run is a process that drives it (D1). This is
what makes "the box died, start another one" a normal operation rather than a recovery procedure.

---

## Web inspector (read-only)

At 2–4 concurrent experiments running 1–8 hours each (D7, D8), a campaign generates a slow trickle
of state that is genuinely unpleasant to follow through CLI polling.

| View | Contents |
| --- | --- |
| Registry | All projects, campaigns, runs. Status, progress, spend, active run |
| Campaign | Leaderboard, metric-over-time chart, budget burn-down, queue depth, active run |
| In-flight | The 2–4 running experiments: stage, elapsed, ETA, live intermediate metrics |
| Experiment | Hypothesis, diff, stage timeline, metrics with intervals, logs, lineage |
| Ideas | Queued hypotheses with priority; rejected ones with reasons |
| Summary | Current research summary, with citations resolving to experiments; version history |
| Events | Raw ledger stream, filterable. The debugging view of last resort |

Read-only in v1 because writes need the actor attribution and confirmation semantics the CLI
already has, and duplicating them in a web form is scope that buys little for a small team.

**The one thing the inspector must do well:** answer "what is happening right now, and is it going
anywhere" in a single screen. That is the question an unattended multi-day campaign actually
raises.

---

## Observability

- **Structured logs** on the controller, correlated by `campaign_id` / `run_id` / `experiment_id`.
- **Metrics** worth exporting: in-flight experiments, queue depth, tick duration, poll latency,
  lease renewals and seizures, infra-failure rate, spend rate, proposer calls and token cost.
- **Alerts** that matter for unattended operation: no active run on an ACTIVE campaign for >10
  minutes; circuit breaker tripped; spend rate above threshold; orphan job detected; projection
  lag growing.

The first alert is the important one. A campaign whose controller died quietly at 2am looks
exactly like a campaign whose experiments are simply slow, and at 8 hours per experiment nobody
notices for a day.

---

## Build order

1. **Ledger + domain.** Append with fencing, projections, replay, state machines. Test with a
   fake in-memory executor and no agents. Nothing else works if this is wrong.
2. **Control loop + lease + recovery**, including the failure-injection suite (doc 04 §6). Still
   no agents: a stub proposer emitting fixed hypotheses is enough to exercise everything.
3. **Command executor**, against a trivial `launch.sh`/`poll.sh`/`find.sh` that runs local
   sleeps. Prove re-attach by killing the controller mid-job.
4. **Coding agent + diff review + sandbox** (doc 08). The first point at which untrusted code runs.
5. **Proposer + research summary**, with mode-collapse defenses from the start — they are not a
   later refinement, they are what makes a pure-LLM proposer viable at all.
6. **Objective, aggregation, confirmation** (doc 07).
7. **CLI, then inspector.**

Steps 1–3 are the whole durability story and can be validated with no LLM in the loop at all,
which is what makes them testable. Do them first and the interesting parts have somewhere solid to
stand.
