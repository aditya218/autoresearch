# Autoresearch v2 — Design

**Status:** Agreed design; v0 built end to end. 2026-08-26.

*Built* (workflows are DAGs by design; the walk implements the linear
case so far): the event ledger with crash recovery, replayed state and materialized
views, the phase contract, the job-script contract with a toy project for CI,
`run_trial` (DAG walk, gates, retries, provenance, resume), the campaign loop
(baseline, admission, budgets, drain, human controls), the pluggable
agent-harness adapter (agentic phases, agent-backed ideation, engine-mediated
`launch_job`), remote-FS mirroring with immediate push on job launch and
restore-from-mirror, the VCS adapters (Mercurial primary, git and plain-copy
secondaries), the repair agent, project skill resolution, and a Claude
Agent SDK harness. CLI: `validate`, `run-phase`, `run-one`,
`run`, `status`. 148 tests.

*Worked example:* `examples/slurm_mle/` is a complete campaign - a small
regression task whose training run is submitted with `sbatch`, polled with
`sacct`, and scored by a harness the trial cannot reach, with the four skills
it names.

*Not yet exercised against reality:* the hg adapter is tested against a fake
`hg` that pins the command surface, not a live repository; the Slurm scripts
are real but have only run against local shims; and no real research project
has run on the engine yet.

## 1. Why this exists, and what it is

Research progress is bottlenecked by experiment throughput, and in a
semi-manual loop the bottleneck is rarely the thinking. Ideas and code are the
fast part; what dominates is everything around them. Long-running jobs get
lost, infrastructure corner cases eat afternoons, results end up in scattered
notes, and every project rebuilds the same scaffolding.

Part of why that scaffolding is so heavy: truly validating an idea is rarely
one experiment. Real workflows put each idea through a **sequence of
evaluations of increasing cost and signal** — a cheap local check, a
small-scale run, then full scale only for the ideas that earn it — with a
judgment call between steps about whether to continue. Run by hand, that is
days of babysitting per idea; it is exactly the structure an engine should
carry.

Agents are now good enough to generate ideas, implement them, and analyze
results; what's missing is a reliable, reusable machine around them.

Autoresearch v2 is that machine: an **autonomous research engine**. Agents
generate ideas grounded in the research so far, push each idea through a
configurable experiment workflow, and everything is recorded durably. The
engine is generic: projects supply configuration, launch scripts, and skills;
the engine supplies orchestration, state, and reliability.

The intent is deliberately dual. At one end, **fully autonomous research**:
the engine ideates, experiments, and learns on its own, for days at a time. At
the other, **a developer tool for human-driven research**: you bring the
ideas, and the same agentic workflow implements, validates, and documents each
one while you do something else. These are not two products — they are the
same machine with ideation turned up or down, and most campaigns will live
somewhere in between: humans seeding directions, the engine exploring around
them.

The architecture in one line: **a small deterministic engine that calls agents
at defined points — never an agent that runs the show.** The engine's behavior
is a pure function of its event log and config, which is what makes budgets,
crash recovery, and audit trustworthy. Agents do the intelligent work —
ideation, implementation, analysis, repair — as bounded episodes whose outputs
the engine validates.

Target operating point: **10s to 100s of trials, running over hours — flexible
in how each experiment runs, with reliability, resumability, and full
auditability** — on a single orchestrator machine.

**The design at a glance** (each item detailed later):

- A campaign runs two loops: an **ideator** keeps a backlog of ideas topped
  up; a **trial runner** pushes each idea through the workflow (§4, §8).
- The **workflow** is a configurable DAG of phases — agentic (driven by
  skills) or deterministic (scripts and remote jobs), with gates — from one
  freeform phase to a fully structured pipeline (§5, §7).
- A **deterministic engine** owns all state: an append-only event ledger plus
  readable views, mirrored to a remote filesystem; a crash means replay and
  resume, never loss (§6).
- **Anything with consequences is validated** — phase results, metric
  provenance, file contracts; prose is inert (§7).
- **Budgets, baselines, a failure taxonomy, and an agentic repair loop** keep
  multi-day campaigns honest and unattended (§8, §9).

## 2. Design principles

1. **Generic engine, project-supplied capability.** The engine never contains
   project-specific logic. Flexibility comes from campaign config, project
   launch scripts, and skills. The engine ships shared phases and tools useful
   across projects.
2. **Never lose a running job.** Remote jobs take hours; the job_id is recorded
   durably before anything else proceeds, and the engine reattaches to
   in-flight jobs after a crash or restart.
3. **Append-only truth.** All state changes are events in an append-only log.
   Corrections are new events, never edits. Everything else is derived and
   disposable.
4. **Minimal and gradual.** Start with the smallest mechanism that meets the
   need (in-memory derived state, no database, one machine). Every deferred
   capability has a designed-in seam, not a rewrite.
5. **Contracts in the engine, intelligence outside it.** The engine provides
   contracts and enforcement (schemas, validation, provenance); intelligence
   lives in skills and agents. Configuration help is a skill, learnings are
   project files read by skills, testing exploits the contracts.

## 3. Core concepts

| Concept | Definition |
|---|---|
| **Campaign** | A long-running autonomous research effort. Its config defines the goal, the metrics that matter, how ideation is done, and the experiment workflow. |
| **Ideation** | The outer loop: an agent generates new ideas based on the research so far, subject to the campaign's ideation config. |
| **Idea / Backlog** | An ideation output waiting to become a trial. The backlog is kept near a configured target size by the ideator loop. |
| **Trial** | One idea run through the workflow — the unit of experimentation. A trial may branch off an existing trial's code state, forming a tree of ideas. |
| **Workflow** | The inner loop: an ordered set of phases that evaluate one idea. Fully configurable per campaign. |
| **Phase** | A logical unit of work: implement the idea, launch an eval job, analyze results. Each declares when it runs, whether it is agentic (skills) or deterministic (job/script), the key metrics it produces, and whether it is a **gate** — failure stops the trial. |
| **Ledger** | The durable record of all research: an append-only event log (truth + audit trail) plus materialized views (the readable current state of every trial). |
| **Job** | Long-running remote work launched via a project-supplied script, tracked by job_id, surviving engine restarts. |

## 4. The life of a campaign

The whole design in one story:

**Setup.** You write `campaign.yaml` — goal, key metrics, ideation settings,
and the workflow's phases — usually with the help of the `configure-campaign`
skill, which drafts the config, runs `validate`, then shows you a visual of the
campaign (the phase DAG, gates, metric bindings, budgets) for a final LGTM.

**Baseline.** The engine starts by running **trial T000: the workflow on
unmodified baseline code**. This is both a smoke test — if T000 fails, the
campaign halts instead of burning budget on a broken workflow — and the
baseline every later trial's metrics are compared against.

**Two loops run.** The **ideator** keeps a small backlog of ideas topped up,
reading the research so far from the ledger's views. The **trial runner**
admits a new trial whenever the backlog is non-empty and budgets allow.

**A trial runs.** The engine acquires a workspace at the parent trial's code
state and walks the workflow DAG: an agentic implement phase edits the code; a
gate checks it's worth continuing; an eval phase launches a remote job — the
job_id is written to the ledger the moment the launch returns — then polls for
hours and collects results; an analysis phase reads them and writes a report.
Every step is an event in the log, and the trial's `trial.json` and the
campaign index are updated as it goes, so `status` always shows the truth.

**The tree grows.** New ideas branch off promising trials; the index shows
every trial's key metrics as deltas vs its parent and vs baseline.

**It ends — or doesn't.** At `max_trials` the engine stops admitting, drains
in-flight trials to completion, and exits cleanly as `budget_reached`. Raise
the budget and restart to continue. A crash at any point is the same story:
restart, replay the log, regenerate anything stale, reattach to running jobs,
carry on.

## 5. Workflows: configuring the experiment

The workflow is where a campaign takes its shape, and it is entirely yours to
configure. A workflow is a DAG of phases — here, one that tries each idea at
small scale first and only spends real compute on survivors:

```yaml
workflow:
  implement:   {agentic: true, skills: [implement-idea]}
  smoke_test:  {after: implement, gate: true, uses: local_eval}
  train_small: {after: smoke_test, uses: slurm_job, params: {gpus: 8}}
  triage:      {after: train_small, gate: true, agentic: true, skills: [assess-promise]}
  train_large: {after: triage, uses: slurm_job, params: {gpus: 64}}
  analyze:     {after: train_large, agentic: true, skills: [analyze-results]}
```

The same shape fits work far from training research — here, serving
optimization, where the metrics are latency and quality and the guarded
resource is a loadtest environment:

```yaml
workflow:
  implement:  {agentic: true, skills: [implement-optimization]}
  benchmark:  {after: implement, gate: true, uses: targeted_benchmark}
  offline_eval: {after: benchmark, gate: true, uses: offline_eval}   # no quality regression
  export:     {after: offline_eval, uses: model_export}
  loadtest:   {after: export, uses: loadtest_job}
  analyze:    {after: loadtest, agentic: true, skills: [analyze-serving]}
```

- A phase is **agentic** (an agent works in the trial's workspace, guided by
  skills) or **deterministic** (a script or a remote job).
- **Gates** stop a doomed trial early — a failed gate ends the trial without
  burning what comes after. A workflow may chain **multiple jobs** at growing
  cost, as above: most ideas die at the cheap step; only survivors reach the
  expensive one. A gate-stopped trial's early results are still in the ledger
  for the ideator to learn from.
- `uses:` references either a **shared phase** from the engine's
  centrally-tested library (launching common job types, running local evals) or
  a **custom phase** the project defines — both built on the same contract.
- **Phases form a DAG**, so a workflow can fan out (several evaluations of one
  trained model) and fan in (compare two seeds, then decide). *v0 implements
  the linear case only*: `after:` takes a single predecessor, and config
  validation rejects a fan-out rather than silently running one branch. The
  ledger already supports the general case, so lifting this is a change to
  `run_trial`'s walk, not to the design (§12).
- Phases declare the metrics they produce; **key metrics** are bound to
  deterministic phases so they can be trusted.

How phases exchange data, the exact output contract, and the shared-phase
library are detailed in §7.

### 5.1 Three ways to run it

Different stages of a project want a different balance of flexibility and
reliability, so the workflow supports three modes — and moving between them is
gradual, never a rewrite.

**Day 0 — exploring: an agent and prompts.** No engine at all. Work in an
interactive agent session with your project's skills and scripts. Maximum
flexibility; nothing is recorded beyond the session and nothing survives a
crash. The right choice while you're figuring out what the experiment even is.

**More flexible — a single agentic phase.** The whole workflow is one agentic
phase, guided by skills. Inside the trial the agent has the same freedom as day
0: improvise, edit code, launch jobs (through the engine's `launch_job` tool),
analyze results. What changes is everything *around* the agent: trials run in
parallel, the ledger records everything, budgets enforce themselves, a crash
resumes instead of restarting, and no job is ever lost. The costs of staying
loose: a trial is a black box while it runs, and metrics are agent-reported —
recorded as `unverified`.

**More reliable — split the workflow into phases and gates.** As the workflow
crystallizes, break it up as in the example above. Each split buys rigor:

- you can see where every trial is instead of waiting hours for a black box;
- gates stop doomed trials before they burn GPU-hours;
- key metrics come from deterministic phases, so they're trusted, not
  `unverified`;
- each job type gets its own targeted repair skill.

The cost is commitment: the workflow now lives in config, so changing its
*structure* means editing config rather than just prompts.

The engine's guarantees — ledger, budgets, baseline, resume, never losing a
job — are identical in the second and third modes; they live in the engine, not
in the workflow. The migration path is incremental: start with one agentic
phase, and promote the steps you notice recurring into real phases one at a
time, adding gates and deterministic evals where reliability matters most.

## 6. The engine

*Overview: one deterministic process per campaign owns all state and calls
everything else. This section covers the ledger, durability, remote jobs, and
version control — the machinery behind "reliable, resumable, auditable."*

One orchestrator process (Python) per campaign runs everything: the two loops,
per-trial workflow state machines (asyncio — 10s to 100s of concurrent
trials), agent invocations, job polling, and ledger sync. Trial execution sits
behind a campaign-agnostic boundary:

```
run_trial(idea, base_node, workflow_config, project) → trial result
```

The campaign loop is a thin shell around it. This boundary is also a product:
the **`run-one` CLI** runs a single hand-written idea through the workflow with
no campaign — the debugging tool, the workflow-setup validator, and the fully
human-curated mode in one. T000 is just `run_trial` with an empty idea. The
engine core beneath it contains no LLM calls, so it tests fast and
deterministically.

### 6.1 The ledger: event log + views

The source of truth is **one append-only JSONL event log per campaign** on
local disk, written only by the orchestrator. Every state change is one event —
one JSON line with a monotonic `seq`, timestamp, type, and payload:

```
{"seq": 481, "ts": "2026-08-26T14:02:11Z", "v": 1, "type": "metric_recorded",
 "trial": "T012", "metric": "accuracy", "value": 0.91}
```

A multi-part state change is a *single* event (e.g. `job_launched` carries both
the job_id and the phase transition), so there is no partially-recorded state.
Writes are append + fsync; on recovery a torn final line is detected and
truncated — the standard write-ahead-log technique. Corrections are new events
(`metric_corrected`), never edits. Events are schema-validated and carry a
version field for format evolution.

Everything else is **derived and disposable**, rebuilt by replaying the log:

- **In-memory state** in the orchestrator — replayed at startup (milliseconds
  at this scale). No database in v0; a SQLite index can be added later as just
  another replay target.
- **Materialized views on disk** — the agent- and human-facing read surface,
  updated on *write*: each state transition rewrites the affected small files
  (`trial.json`, the campaign index) via atomic rename. Ideation reads
  `index/trials.json` and drills into trials; analysis phases read their own
  trial directory; nobody but the engine ever reads the raw log. Each view
  records the `seq` it was materialized at, so a stale view is detected and
  regenerated at startup.

On disk:

```
campaign/
  campaign.yaml               # config: goal, metrics, ideation, workflow
  ledger/
    events.jsonl              # append-only source of truth (engine-only)
  trials/
    T012/
      trial.json              # materialized: status, parent, phase states,
      idea.md                 #   metrics, job_ids, timestamps — always current
      phases/
        implement/            # per-phase artifacts: diffs, logs
        eval/                 #   raw results
        analyze/report.md     #   agent-written analysis
  index/
    trials.json               # one row per trial: id, parent, status, headline
                              #   metrics — "research so far" at a glance
```

### 6.2 Storage tiers and sync

- **Local disk = working tier.** The engine and agents read and write only
  local files.
- **Remote filesystem = durability tier.** A lagging mirror of the campaign
  directory; nothing reads it in normal operation.
- **Sync = an asyncio task** doing an incremental pass every *k* seconds: the
  log's new bytes plus files changed since last pass. Artifacts are immutable
  once written, so they copy once. `job_launched` events trigger an immediate
  push instead of waiting. Sync is idempotent; a slow or failed pass never
  blocks research.
- **Disaster recovery:** local disk lost → restore the directory from remote,
  restart → replay, regenerate stale views, reattach to in-flight jobs. Worst
  case: k seconds of bookkeeping lag; no lost jobs.

### 6.3 Remote jobs

The engine's contract with a project's job system is three scripts —
**launch** (prints a `job_id`), **poll** (`job_id` → status), **collect**
(`job_id` → results) — plus two optional ones: **find** (`tag` → job_ids)
and **cancel** (`job_id`). Cancel matters more than it looks: without it, a
killed trial, a repair that gives up, or a relaunch all leave the old job
queued and consuming an allocation nobody is watching. Poll answers `pending`, `running`, `done`, or `failed`;
anything else is a situation the engine has no rule for and goes to repair.
`pending` is worth its own value because a job that has never started is a
different problem from one that has run a long time - it has consumed nothing,
there is nothing to salvage, and it may never be placeable as submitted - so
a phase can give the queue its own patience (`pending_after_polls`). The engine appends `job_launched` the moment launch
returns and pushes it to remote immediately. After a restart, replay yields
every in-flight job_id and polling resumes. The engine knows nothing about the
underlying job system.

Two small additions make ambiguous launches recoverable (§9): the engine passes
launch a **tag** (campaign/trial/phase id) to attach to the job, and projects
may supply an optional fourth script, **find** (`tag → job_ids`), so "did that
launch actually create a job?" becomes a lookup instead of a mystery.

### 6.4 Version control (designed for Mercurial-style VCS)

A trial's implementation branches from its parent trial's code state, giving
the tree of ideas a concrete code lineage. Three Mercurial-driven rules:

1. **Nameless VCS:** code states are immutable commit hashes (nodes), never
   branch names or bookmarks. The ledger maps `trial_id → node`; the VCS
   interface only speaks hashes. (On git this is detached-HEAD checkouts.)
2. **Workspaces have acquire/release semantics** and may be pooled — hg uses
   the `share` extension; some hg-compatible clients create workspaces their
   own way and are heavier still. The engine never
   assumes acquisition is cheap.
3. **No staging area:** commit means "commit everything in the workspace"
   (`hg commit -A`) — the right semantic for agent-produced changes.

The interface is ~5 operations (`workspace_acquire(base_node)`,
`commit_all`, `current_node`, `diff`, `workspace_release`). v0 ships the hg
adapter as primary, with the client binary and workspace creation as
parameters so an hg-compatible client needs no new adapter; git is a trivial
secondary.

## 7. Phases in detail

*Overview: §5 introduced what phases are; this section defines how they talk
to the engine and to each other, the shared-phase library, and why recorded
metrics can be trusted.*

**The agent substrate is pluggable.** Agentic phases run on an off-the-shelf
agent harness — any SDK that provides the agent loop with filesystem tools,
shell, and a skills/prompt mechanism — behind a thin adapter:
`invoke(prompt, skills, workspace, tools) → outputs`. The engine is not tied
to a specific vendor or model; the harness is a per-deployment choice, and
harnesses can be swapped without touching the engine. (Chosen over hosted
managed-agent services, whose remote sandboxes fight local workspaces and
internal launch scripts, and over a hand-built agent loop, which would mean
rebuilding file tools and skills.)

### 7.1 The phase contract

A phase communicates with the engine through exactly one file: `result.json`
in its phase directory, schema-validated:

```
{status: "passed" | "failed", metrics: {name: value}, notes: str, artifacts: [paths]}
```

Agentic phases are instructed to write it (a malformed one is a retryable
error); for job phases the collect script produces it. The engine does all
ledger work — phases never touch the ledger. Transitions stay dumb: a phase
becomes ready when its predecessor passes; a gate's `failed` ends the trial as
`gate_stopped`.

**One rule governs phase outputs: anything with consequences is validated;
anything that is prose has no consequences.**

- **Decisions read validated fields, never text.** Advancing the trial, gates,
  and recorded metrics all come from `result.json`. The engine never
  interprets a sentence like "it looks good, roughly 0.91" to decide anything.
- **Files a later phase needs must verifiably exist first.** A phase declares
  what it `produces:`; the engine checks when the phase *completes*, and a
  missing file fails the producer. A later phase never starts with an expected
  input missing — no room for an agent to "helpfully" fabricate one. Phases
  write only their own directory; earlier phases' outputs are read-only.
- **Free text is welcome, but inert.** `notes` and analysis reports are
  context for later agents and the ideator. A wrong sentence there can't flip
  a gate, record a metric, or conjure a file.

### 7.2 Shared vs custom phases

The engine ships a **standard library of phases** for common actions — e.g.
deterministic phases to launch Slurm jobs, run local in-process
evals, or launch jobs on a serving cluster — centrally tested and reused via
configuration, with coverage growing over time. Projects add custom phases
wherever the library doesn't fit. The rule that keeps this healthy: **shared
phases are not special.** They are built against the same public contract as
custom ones, and the library lives *beside* the engine, not in it — the engine
core never learns what Slurm is. In config both look alike (§5's example).

`uses:` resolves to one of four things: `local` and `job` (the project's own
scripts), a shared phase shipped beside the engine (`slurm_job`), or a path
to a custom directory (`./phases/my_sim`). Anything else is an error rather
than a silent fallback, so a typo fails at `validate` instead of running the
wrong thing. A shared phase is reusable because what it runs is a parameter:

```yaml
train:
  uses: slurm_job
  params:
    command: "python eval/train.py --workspace {workspace} --out {out}"
    partition: gpu
    gpus: 8
```

If a shared phase needs something the contract doesn't offer, that's a
contract gap to fix for everyone — not a private hook. Agentic phases
generalize the same way, with skills as the payload: shared `fix-builds` or
`analyze-results` skills are centrally-maintained phase definitions any
project could have written. Each library phase ships with its own `run-phase`
fixture tests (§10.3), and the `configure-campaign` skill knows the catalog —
shared phases first, custom scaffolding only when nothing fits. Coverage grows
from real demand, not speculative breadth.

### 7.3 Metric integrity

Skills raise the floor on agent behavior but cannot guarantee honest numbers —
gamed or hallucinated metrics are exactly the failure mode prompts can't
prevent. The engine enforces **provenance**:

- Every metric event records the producing phase and whether it was agentic or
  deterministic.
- Campaign config binds each **key metric** (consumed by gates and ideation)
  to a deterministic phase: `accuracy: {from: eval}`. A key metric from any
  other phase is rejected. Agentic phases may report numbers; they land as
  `unverified` and views label them so.
- **Project launch/eval scripts live outside the trial workspace:** the agent
  edits training code — its job — but cannot rewrite the eval harness or the
  launcher that invokes it.

Residual risk stated honestly: an agent can still overfit the eval set through
legitimate code changes. That is a science problem, mitigated by skills and
the analysis phase — not an integrity problem the engine can solve.

## 8. Ideas and budgets

**Governing rule: every budget and limit is derived from the ledger by replay —
never from an in-memory counter.** Restart, crash recovery, and deliberate
resume are then the same code path, and budget correctness across them is
automatic.

**The ideator loop (producer).** When pending ideas fall below
`ideation.backlog_target`, it reads the research so far (`index/trials.json`
and drill-downs) and generates ideas until the backlog is refilled. Each idea
is an `idea_created` event plus an `idea.md` recording the idea, its
rationale, the trials it builds on, and the ledger `seq` it was generated at —
auditable: what did the ideator know?

The ideator is **load-bearing**: it is the only component that reads
everything, so the views are effectively its API and gate campaign quality;
its config (model, effort, skills, prompt) is as rich as phase config; and its
failures are isolated — running trials continue, the backlog drains, the
engine retries. It never crashes a campaign; but "never stalls" has a limit
worth stating: with nothing running, an empty backlog, and ideation failing
repeatedly, the loop stops for a human rather than spinning forever. Keep `backlog_target`
small (2–5) so ideas stay fresh. And ideation is **a dial, not a
prerequisite**: with idea injection (§10.1) the engine is fully useful as a
"run my ideas rigorously" machine with autonomous ideation at zero.

**The trial runner (consumer).** Admits a new trial when all of these hold,
then emits `trial_created` (consuming the idea):

```
backlog non-empty
AND in_flight_trials < active_trials                  # concurrency limit
AND terminal_trials + in_flight_trials < max_trials   # total budget
```

**`active_trials`** caps concurrency. Because phases within a trial are
sequential, it also coarsely caps resource use — if each trial's heaviest job
needs a quarter of the available capacity, `active_trials: 4` keeps the
campaign inside it. Deliberately coarse for v0; per-phase resource pools can
be added later at this same admission seam.

**`max_trials`** counts in-flight trials to prevent overshoot. Terminal =
workflow finished or gate-stopped — both consumed an evaluation; infra-errored
trials don't count. At budget: stop admitting, **drain** in-flight trials,
mark the campaign `budget_reached`, exit cleanly — nothing is killed.
**Resume = restart:** raise `max_trials` and start the engine; replay
recomputes counts and continues. The same replay resumes in-flight trials
mid-workflow — completed phases never re-run; launched jobs reattach by
job_id.

**Baseline (T000).** Run at campaign start on unmodified baseline code: smoke
test (campaign halts if it fails) and the reference point — the index shows
every trial's key metrics as deltas vs parent and vs baseline. (Replicate runs
for variance estimation: deferred.)

## 9. When things go wrong

Classify failures by *who can tell what happened*; every classification is a
correctable event, and every retry is in the audit trail.

- **Engine-detectable infra errors** (exceptions, SDK/API errors, a launch
  exiting non-zero without a job_id, malformed `result.json`): unambiguous.
  Retry with backoff up to per-phase `max_retries` (default 2); exhausted →
  trial ends `errored`, doesn't count against `max_trials`, campaign
  continues.
- **Job-reported failure** (poll says the job died): inherently ambiguous —
  flake, or the idea's fault (an OOM the idea caused). The poll script may
  return a category; absent that, **default = idea failure** (conservative:
  counts against budget). The analysis phase, which sees the logs, can
  reclassify via a `trial_reclassified` correction event — replay-derived
  budgets retroactively correct.
- **Gate failure:** the workflow working as designed. Never retried.
- **Quota/credit exhaustion:** a distinguished error — retrying is pointless.
  The engine pauses admission and agentic phases, marks the campaign
  `stalled_quota`, and keeps polling remote jobs (token-free). Restart when
  quota returns resumes via normal replay.

### 9.1 The repair agent

Remote jobs also fail in odd ways no rule set can anticipate. Two examples of
the shape:

- A job's status never updates to "complete" — but the logs show it did enough
  work. The results are usable; the right move is to collect them anyway.
- A job dies in a way that anyone familiar with that system recognizes as
  transient: it just needs a restart.

Rules can't cover this long tail, but an agent reading the logs can. When a
job phase hits a situation the engine has no rule for (ambiguous launch, stuck
or unknown status, missing results), the engine calls a **repair agent**. It
investigates — logs, the job's history from the ledger — and recommends one
action:

> collect the results anyway · relaunch · keep waiting · fail as infra ·
> fail as idea · give up (ask a human)

The engine performs the chosen action and records it. The agent only ever
*recommends*; it never launches or kills jobs itself, so a repair can never
create a job the ledger doesn't know about.

- Each phase names a repair skill for its job type
  (`repair: {skill: repair-slurm-train, max_attempts: 2}`) — where a project
  writes down what it knows about how that system fails.
- After `max_attempts`, the trial parks as `needs_attention` for a human. A
  trial repair *rescued* is not flagged — the repair stays visible in the
  trial's view, but only trials repair gave up on raise a flag, or every
  rescue would cry wolf.
- The exact triggers and actions are meant to be tuned against whatever
  failures a given deployment actually sees.
- Repair feeds self-improvement (§10.4): incidents accumulate in the ledger,
  and promoting recurring patterns into repair skills — with human review — is
  the learnings loop applied to infrastructure.

## 10. Operating and improving

### 10.1 Observe and intervene

- A **`status` CLI** reads the views: campaign state, per-trial phase/status,
  metric deltas, stalls, `needs_attention` items.
- **Minimal human-in-the-loop**, each just an event: pause/resume the
  campaign, kill a trial, inject an idea into the backlog.
- Richer approvals (gate overrides, mid-campaign checkpoints) are deferred.

### 10.2 Configure

- **Enforced by the engine:** a typed config schema plus a `validate`
  command — DAG acyclic, key metrics bound to deterministic phases, scripts
  exist and are executable, skills resolve, workspace acquisition works. T000
  is the runtime validator.
- **Intelligent help is a skill:** the independent `configure-campaign` skill
  interviews you, drafts `campaign.yaml`, runs `validate`, iterates.
- **Visual LGTM:** once the config validates, the skill renders a visual of
  the campaign — DAG, gates, metric bindings, budgets, ideation settings — and
  asks you to approve before the first run. The confirmation is recorded.

### 10.3 Test

- **Unit:** replay, admission, event serialization — pure functions.
- **Phase-level:** `run-phase` executes one phase against a fixture directory —
  also how projects develop their skills and scripts in isolation.
- **Workflow-level:** a shipped **toy project** (launch echoes a fake job_id,
  poll counts down, collect writes `result.json`) lets CI run full campaigns
  in seconds.
- **Crash/recovery:** kill the process after seq N, restart, assert derived
  state converges and no job is orphaned — event sourcing makes the hardest
  orchestrator property scriptable.

### 10.4 Learn

- **Within a campaign**, self-correction happens by construction: analysis
  reports are durable and the ideator reads them.
- **v0, across campaigns:** a project-specific `learnings.md`, updated with
  **human review**, referenced by the project's skills. Zero engine change.
- **Future, first-class:** automated learnings are unverified conclusions —
  like agent-reported metrics, but compounding (a wrong learning biases
  ideation, which produces trials that "confirm" it). The deferred shape: a
  campaign-end **retrospective phase** drafts candidate learnings with
  citations to trial evidence; ledger provenance (`learning_proposed` /
  `learning_accepted`); **human-gated promotion** into the project directory;
  accepted learnings surface in views and ideator context across campaigns.

## 11. Decisions log

| Decision | Choice | Alternatives considered |
|---|---|---|
| Agent substrate | Off-the-shelf agent harness behind a thin adapter; pluggable, self-hosted | Hosted managed-agent services (remote sandbox fights local scripts/workspaces); hand-built agent loop (rebuild file tools + skills) |
| Language | Python | TypeScript |
| Orchestration | Deterministic engine; agents invoked at defined points, returning validated verdicts | Agent-as-orchestrator (unreplayable, expensive, compounding per-decision error) |
| Ledger | JSONL event log + materialized views; no DB in v0 | SQLite primary (not agent-readable, risky on network FS); files-only without event sourcing (hand-rolled transactionality) |
| Durability | Local working tier, remote FS mirror via periodic incremental sync + immediate push for job launches | Working directly on remote FS (slow; SQLite locking unsafe there) |
| VCS | Mercurial-first: nameless node-hash interface, acquire/release workspaces; hg adapter primary, client binary parameterized | git-first design (branch-name semantics don't map to hg); git remains a trivial secondary adapter |
| Scale envelope | One orchestrator process/machine, asyncio, 10s–100s of concurrent trials | Multi-machine (deferred; seam = ledger interface + stateless phase execution) |
| Budget accounting | Derived from ledger by replay; at budget, drain and exit cleanly; resume = restart with raised budget | In-memory counters (break on restart); killing in-flight trials at budget |
| Concurrency | Single `active_trials` limit at trial admission | Per-phase resource pools / semaphores (premature for v0; the admission check is the seam to add them later) |
| Metric integrity | Provenance enforcement: key metrics bound to deterministic phases; agentic numbers `unverified`; project scripts outside the workspace | Skills-only (can't guarantee); sandboxing (heavy for v0) |
| Baseline | T000 = workflow on unmodified base code; campaign halts if it fails; index shows deltas vs parent and baseline | No baseline (signal vs noise indistinguishable); replicate runs (deferred) |
| Ambiguous job failure | Defaults to idea-failure (counts against budget); analysis reclassifies via correction event | Default-infra (budget never binds); automatic classification (not generically possible) |
| Phase contract | Single validated `result.json` per phase; engine owns all ledger writes | Phases emitting events directly; ad-hoc output parsing |
| Trial execution | `run_trial` as a campaign-agnostic boundary; `run-one` CLI; campaign loop is a thin shell | Trial logic entangled with the campaign loop (untestable standalone) |
| Flexible mode | Freeform campaign = one agentic phase + engine-mediated tools (`launch_job`); guarantees identical, metrics `unverified` | Separate fully agentic engine (loses ledger trust, replay, crash recovery — the properties the project exists for) |
| Phase library | Shared phases/skills beside the engine, on the same public contract as custom ones; grown from real demand | Privileged built-ins (makes custom phases second-class); speculative breadth |
| Repair | Per-phase repair skills for extraordinary job failures; diagnose read-only, mutate via engine-executed verdicts; park `needs_attention` after max attempts | Rules-only (long tail unhandled); agent executing fixes directly (could orphan jobs) |
| Config help | Independent `configure-campaign` skill + engine `validate` + visual LGTM | Bespoke configuration agent inside the engine |
| Learnings | v0: human-reviewed project `learnings.md`; future: retrospective phase + human-gated promotion with ledger provenance | Auto-written learnings without review (compounding bias risk) |

## 12. Open questions (deferred deliberately)

- **First concrete project** — the campaign that validates the design; config
  schema details get finalized against it.
- **Parallel phases** — the design is a DAG; v0 implements the linear case,
  because every workflow written so far has been a chain. Lifting it means
  `after: [a, b]`, a ready-set walk in place of the cursor in `run_trial`,
  and running independent phases concurrently within a trial. The ledger
  needs no change — phase events already carry their own identity — and
  until then config validation refuses a fan-out rather than running one
  branch silently.
- **Replicate runs** — variance estimation for headline metrics; v0 records
  deltas vs parent/baseline only.
- **Idea staleness** — should the ideator revise or cancel pending backlog
  ideas when new results land? (v0 mitigation: small backlog target.)
- **Per-phase resource limits** — if the coarse `active_trials` cap wastes too
  much capacity, add named resource pools + per-phase `requires` at the
  admission seam (bookkeeping quota, not cluster queries).
- **Snapshots** — only if log replay ever gets slow; the format allows adding
  them without migration.
- **Multi-campaign / multi-machine** — one log per campaign keeps
  single-writer clean; cross-campaign views are multi-log replays.
