# Decisions

All 26 decisions resolved, with rationale and consequence. Remaining unknowns are at the bottom —
none of them block implementation.

One decision has been revised since it was first taken: **D2**, from event sourcing to mutable
state plus a debug log. The original rationale did not survive scrutiny; the reasoning is recorded
in full below rather than quietly replaced.

---

## Core architecture

**D1 — `run` is a leased execution session, not an owner of experiments.**
Experiments carry `created_by_run_id` for provenance; each stage attempt records the run that
drove it. *Because:* a crash-resume must produce a new run without orphaning in-flight work, which
ownership makes impossible. *Consequence:* concurrency safety must come from a lease, and it does
(doc 04 §1).

**D2 — Mutable state tables are the source of truth; an append-only transition log exists for
debugging only.** *(Revised — the original decision was full event sourcing.)*

*Because:* the durability guarantees turned out to be orthogonal to event sourcing. Intent-before-
effect is committing a row before calling `launch.sh`; idempotency is a unique constraint; fencing
is a `WHERE` clause; the resume point is `SELECT … WHERE state IN (non-terminal)`. None of them
need a log. And history is largely free anyway — one row per experiment and per stage attempt,
retained forever. What an event log uniquely provides is *transition* history, which a small
append-only table supplies without a projection layer, replay code, or a rebuild-equivalence test.

*Consequence:* two rules keep the log from silently becoming load-bearing again — it is written in
the same transaction as the state change (a best-effort log drifts precisely during the incidents
you need it for), and **nothing in the control path reads it**. Anything the control loop needs is
a column, not a count over log rows. Enforceable as a lint rule against `control/` importing the
log reader.

*What was given up:* point-in-time reconstruction of the whole ledger. Mitigated by storing
`proposer_context` verbatim, so past decisions remain inspectable even though the surrounding
state is not rebuildable.

**D3 / D8 — Parallel experiments, 2–4 in flight, proposer sees the unresolved frontier.**
*Because:* at 1–8 hours per experiment, strict sequencing wastes most of the day; beyond ~4 the
proposer's reasoning about pending work degrades faster than throughput improves. *Consequence:*
the proposer brief carries an `in_flight` block with no results in it.

**D4 / D10 / D11 — Stages are either local or external jobs; external jobs are user-supplied
commands, and exactly-once launch is required.**
Required: `launch`, `poll`, `find`, plus `logs` where triage is enabled. Optional: `cancel` (D24),
`progress`. The engine passes `AUTORESEARCH_IDEM_KEY`; the launcher tags the job with it; `find`
recovers it after a crash. *Because:* this is the only way to re-attach to
an 8-hour job rather than relaunch it. *Consequence:* a missing `find` command is a spec error,
and a lint rule caps local stages at 20 minutes so expensive work cannot hide in-process —
`implement` being the one sanctioned exception at 60m (D26).

**D9 — Domain-agnostic core.**
The engine submits a command, polls a job_id, and reads a metrics file. *Because:* the two named
projects (v4 latency, pretraining quality) already have different inner loops. *Consequence:*
everything domain-specific lives in user scripts, and the metric registry is the only typed
contract between them and the engine.

**D22 — Python, minimal dependencies, no workflow framework.**
*Because:* the durability contract is the product; delegating it to Temporal would create a second
source of truth competing with the state tables. *Consequence:* the failure-injection suite is mandatory.

## The experiment

**D5 — An agent writes and runs code. Branch per experiment off a pinned base commit.**
*Because:* the interesting changes in both named projects are structural, not parametric.
*Consequence:* the entire safety model (doc 08) enters v1 — sandboxing, protected paths, diff
review, credential scoping. This is the largest single cost of the decision and it is not
optional: the engine builds and runs agent-authored code unattended, hundreds of times.

**D7 — Experiments take 1–8 hours.**
*Consequence:* re-attach is essential rather than nice-to-have; a campaign runs for days, so
provenance drift is a real risk and the controller must survive infrastructure moving underneath
it. Early-kill would be the other major consequence, but it is gated on cancel (D24).

**D6 — Pure LLM proposer, no classical sampler.**
*Because:* with code-level changes the search space is mostly structural, where a sampler has
little to grip. *Consequence:* mode-collapse defenses become mandatory — exploration quota, family
saturation tracking, diversity escalation, diversity-collapse stop. Without a sampler injecting
diversity, the engine must supply it deliberately.

**D23 — Constrained scalar objective, no Pareto frontier.**
One primary metric, others as guardrails. *Consequence:* the leaderboard stays a ranking; a
guardrail violation makes an experiment infeasible, not failed.

**D26 — The coding agent iterates on mechanical failures; semantic failures surface untouched.**
`build` and `test` are tools inside the agent's sandbox, capped at `max_repair_iterations`
(default 3). *Because:* a compile error says nothing about whether the hypothesis was good, and
surfacing it as a negative result teaches the proposer that a direction is dead when the agent
merely fumbled the syntax.

*The line* is not effort or size but whether the change has started producing evidence: compile,
lint, import, and patch-apply errors mean the change does not yet exist in runnable form, so the
agent may iterate. Failing tests, diverging numerics, and a regressed metric mean the change ran —
that is the finding, and retrying it is how you get an agent that makes tests pass by any means
available.

*Consequences.* The loop lives **inside** the `implement` stage rather than as a DAG cycle, so
resumption semantics are untouched; `implement` gets a raised 60m local ceiling as the one
sanctioned exception, accepting that a controller crash discards up to an hour of agent work
(tokens and build time, not GPU-hours). The old `build` stage becomes `verify_build`, a clean-room
build of the committed SHA that catches "it built in the dirty worktree but not from a fresh
checkout".

*The risk this introduces* is not test-hacking but **hypothesis drift**: an agent that cannot make
the fusion compile may converge under repair pressure on something that compiles and no longer
fuses anything, after which eight GPU-hours measure a near-no-op and the campaign records "kernel
fusion: no effect". That false null is worse than a failure — it looks exactly like a finding, and
it closes a direction that was never tested. Guarded by a fidelity check in `review` comparing the
final diff against the `change_spec`, before any job launches.

*Hence a new outcome:* `could_not_implement`, distinct from `experiment_failure`. It is reported
to the proposer as "not tested" rather than "does not work", and it does not count toward family
saturation — clustered implementation failures mean a direction is hard to express, not that it is
unpromising.

## Campaign semantics

**D13 — Strict campaign isolation, with a fork carve-out.**
A campaign sees only its own experiments, except a forked campaign inherits its parent's ledger
read-only. *Because:* isolation makes a campaign a real control; a fork is a continuation, not an
independent trial, so making it rediscover the parent's work is pure waste. *Consequence:* the
result cache is per-campaign, so two campaigns may pay twice for an identical diff. Accepted as
the honest price of the control.

**D18 — Config immutable for measurement and execution; editable for search policy and budget.**
Frozen: objective, metrics, workflow, replication, provenance pins, protected paths. Editable in
place: budget, concurrency, proposer settings, stopping criteria, approval gates. *Because:* the
frozen set is what makes "is A better than B" well-formed; the editable set changes only how the
*next* idea is chosen, which no past result depends on.

**D14 — Humans may inject hypotheses and steer the proposer mid-campaign.**
Injected ideas bypass the exploration quota but not safety checks. Steering notes appear in every
subsequent brief. *Because:* it is the cheapest possible intervention and frequently rescues a
campaign that has gone down a dead end.

**D12 — LLM-maintained research summary, citations enforced, drift-audited.**
Every claim cites experiment_ids, checked by a validator. Every 25 experiments the summary is
re-derived independently and diffed. Versions retained so a bad pass can be rolled back.
*Because:* it is the only thing keeping a multi-hundred-experiment campaign coherent past the
context limit, which makes its integrity load-bearing.

**D19 — Deliverable is a report plus the winning branch.** The engine does not open PRs.
*Consequence:* it never needs write access beyond pushing `autoresearch/*` branches.

**D24 — No cancel in v1. Jobs run to completion.**
*Because:* the kill tool exists but is not worth wiring up yet; some wasted resources are
acceptable. *Consequence, stated plainly:* **every stop path gates admission, not execution.**
Pause, stop, budget exhaustion, the circuit breaker, and the kill switch all halt new work and
none of them halt running work. Budget becomes a soft ceiling with an overshoot of up to
`concurrency × max_experiment_cost` (doc 08 §4), `kill_criteria` is recorded but not enforced, and
the orphan reaper becomes read-only — which incidentally removes the risk of it killing a job it
should not have. Stopping a campaign now requires the controller to stay alive *longer*, draining
in-flight work rather than abandoning results already paid for.

**D25 — Failure classification is a status map, then an agent that reads logs, then a hard
ceiling.**
Unambiguous statuses (`preempted`, quota errors) map directly. Ambiguous ones (`error`, `failed`)
escalate to a triage agent that fetches the log and returns a class plus a verifiable evidence
quote. *Because:* the distinction decides both retry behaviour and what the proposer learns, and
status alone cannot supply it. *Consequence:* `logs` becomes a required command, and job logs
become untrusted input to an LLM — written by code the coding agent authored, which has an
incentive to be classified `infra` and earn a retry. The `max_infra_reclassify` ceiling stays
absolute regardless of the verdict; it is the only control that bounds the damage rather than
merely raising the cost (doc 08 §3).

## Operations

**D15 — Scheduling delegated entirely.** The engine tracks concurrency slots and budget; your
scheduler places jobs. *Consequence:* quota and preemption logic stay out of scope.

**D16 — Postgres for the log and queryable state; distributed FS for artifacts** at
`{project_id}/{campaign_id}/…`, path-addressed from IDs. *Because:* content-addressed storage was
solving a problem that does not exist here. *Consequence:* three invariants — the FS is never
authoritative for state, write-then-record, and one directory per stage attempt.

**D17 — Hundreds of experiments per campaign, tens of campaigns per project.**
*Consequence:* single Postgres instance, no partitioning, and the registry rollups stay plain SQL
views rather than maintained tables. Partitioning is documented as a migration, not built.

**D20 — Small trusted team, shared instance.** Attribution everywhere, authorization nowhere.
*Consequence:* adding authorization later is additive, because attribution is recorded from day
one.

**D21 — CLI plus read-only web inspector.** *Because:* an unattended multi-day campaign is
genuinely painful to follow through CLI polling, and the inspector must answer "is this going
anywhere" in one screen.

---

## Still open — none blocking

1. **Sandbox implementation.** Container runtime vs. namespaces vs. rlimits-only for local stages
   depends on what the host environment supports. Doc 08 specifies the policy; the mechanism is an
   implementation choice.
2. **Job name constraints.** Whether the launcher accepts a settable job name, and its length and
   character limits. The `ar-{campaign}-{experiment}-{stage}-{attempt}` key assumes it does; a
   truncated-hash fallback works if not.
3. **Status vocabulary.** The exact set your polling tool returns, to fill in the `status_map`.
   Known so far: something for `error`, `preempted`, and `failed`.
2. **Which repo and base commit** the first campaign targets, and what its protected-path list is.
3. **Embedding model for near-dedup**, and the similarity threshold. Needs calibration against
   real proposals — set it from observed duplicate rates in the first campaign rather than
   guessing now.
4. **Cost accounting source.** Whether `poll` reports cost, or the engine derives it from
   duration × hardware class. Affects budget accuracy but not the design.
5. **Held-out evaluation mechanics** for the first project: what the held-out set is, and how it
   stays invisible to the coding agent's working tree.
6. **Retention policy** for experiment artifacts — how long non-winning experiments' logs and
   checkpoints survive.
7. **Default threshold calibration** throughout: staleness (10), exploration fraction (0.3),
   family saturation (5), convergence (40), infra reclassify (3). All are guesses until a real
   campaign runs. They are config, not constants.
