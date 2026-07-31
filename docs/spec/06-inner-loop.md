# 06 — Inner Loop: Workflow Specification and Execution

The inner loop is the user-configured workflow for a single experiment, compiled to a durable
state machine. An experiment is complete only when the workflow has been exercised to a terminal
state.

Per D5, an experiment begins with a **coding agent writing a change on a git branch**; per D10,
expensive work is launched by **user-supplied shell commands**; per D9, the engine never learns
what the job actually does.

---

## Git branch as the unit of change

```
base_commit (pinned in campaign.config.provenance_pins)
    └── autoresearch/{campaign_id}/{experiment_id}      ← agent commits here
```

Rules:

1. **Every experiment branches from the same pinned `base_commit`**, not from the previous
   experiment's branch. Otherwise experiments compose accidentally and comparability is lost.
   Building deliberately on a prior result is expressed by the proposer citing it and the agent
   re-implementing it, or by forking the campaign with a new base commit.
2. **The commit SHA is the experiment's identity.** `resolved_config_hash = H(commit_sha,
   launch_parameters)`. A cache hit requires both to match.
3. **The diff is the primary artifact.** Stored at
   `{root}/{project_id}/{campaign_id}/experiments/{experiment_id}/diff.patch`, referenced from
   the ledger, and shown to the proposer as evidence.
4. **Diff-level dedup.** If the agent produces a diff byte-identical to a previous experiment's,
   short-circuit with `ExperimentResultReused` before launching anything. With a pure-LLM
   proposer (D6) this fires more often than you would expect, and it is free money.
5. The agent may push only to `autoresearch/*` branches. See doc 08.

---

## Workflow spec

A DAG of stages, versioned by content hash. Declarative, so the engine can schedule, resume, and
re-attach without executing user code to discover what happens next.

```yaml
name: latency-opt
version: 3                        # hashed into workflow_version

stages:
  - key: implement
    kind: local
    handler: autoresearch.agents.coding_agent      # writes the change, commits to the branch
    timeout: 60m                                   # raised ceiling — see lint rule 2
    tools: [read, write, build, test]              # iterates internally against build/test (D26)
    max_repair_iterations: 3
    retries: { max: 1, on: [infra] }

  - key: review
    kind: local
    handler: autoresearch.safety.diff_review       # protected paths, secrets, fidelity — doc 08
    needs: [implement]
    timeout: 5m
    on_reject: abort_experiment

  - key: verify_build
    kind: local
    sandbox: subprocess                            # clean checkout of the commit, not the worktree
    command: ["make", "build"]
    needs: [review]
    timeout: 10m
    failure_class: experiment                      # the agent said it built; from clean it did not

  - key: train
    kind: external_job
    needs: [build]
    executor: command
    launch:  "./scripts/launch.sh --commit {{ commit_sha }} --config {{ config_path }}"
    poll:    "./scripts/status.sh {{ job_id }}"
    # cancel: optional in v1 (D24) — jobs run to completion
    find:    "./scripts/find.sh --tag {{ idem_key }}"   # optional; see doc 04 §2 tiers
    timeout: 8h
    poll_interval: 60s
    retries: { max: 3, on: [infra] }
    outputs:
      metrics: "{{ artifact_dir }}/metrics.json"   # schema: project.metric_registry

  - key: analyze
    kind: local
    handler: autoresearch.agents.analyst
    needs: [train]

terminal: [analyze]
```

### Lint rules, enforced at spec-validation time

1. **DAG, no cycles.** Loops are retries or the outer loop, never workflow edges — otherwise
   resumption has no well-defined position.
2. **`local` stages must be cheap.** A declared `timeout` above the local ceiling (default 20m)
   is a spec error. This is what makes the durability guarantee real rather than aspirational:
   a controller crash re-executes a local stage from scratch. If your build takes 40 minutes,
   it is an `external_job`.

   **`implement` is the one sanctioned exception**, with a ceiling of 60m, because internal
   repair iteration (D26) means an agent turn plus up to three builds. The trade is explicit: a
   controller crash mid-implement discards up to an hour of agent work. That is LLM tokens and
   build time, not GPU-hours, and controller crashes are rare — cheap enough to accept, and far
   cheaper than the alternative of making a sandboxed multi-turn agent session re-attachable.
3. **Every `external_job` must declare `launch` and `poll`.** `find` is optional but recommended;
   without it the engine falls back to the receipt file, and without that to relaunch-and-flag
   (doc 04 §2). Which tier a workflow lands in is recorded on the campaign, so a report can state
   honestly what its recovery guarantee was. `cancel` is likewise optional (D24).
4. **Exactly one stage produces metrics**, and its output must typecheck against the project
   metric registry. An experiment that cannot produce comparable metrics is not an experiment.
5. **`retries.on` may only list `infra`.** Retrying an `experiment` failure re-runs a
   deterministic failure and burns budget for no information.
6. **Stage keys are stable across workflow versions** where semantics are unchanged — resumption
   and cross-version comparison both key on them.

---

## The command executor

The primary executor (D10). The engine's entire knowledge of your infrastructure is four
commands and a metrics file.

### Contract

| Command | Receives | Must print | Exit code |
| --- | --- | --- | --- |
| `launch` | `AUTORESEARCH_IDEM_KEY`, `commit_sha`, `config_path`, `artifact_dir` in env | the `job_id` on stdout, alone on the last line | 0 on successful submission |
| `poll` | `job_id` | one of `PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED`, `KILLED` | 0 if the status was determined |
| `cancel` *(optional, D24)* | `job_id` | — | 0 if cancelled or already terminal |
| `find` *(optional, recommended)* | `AUTORESEARCH_IDEM_KEY` | the `job_id` if a job carries this tag, nothing otherwise | 0 either way |

The engine always passes `AUTORESEARCH_IDEM_KEY` to `launch.sh`. What the launcher does with it
selects the recovery tier in doc 04 §2: write it as a receipt filename (recommended default), tag
the submitted job with it so `find` can retrieve it (strongest), or ignore it (weakest, recovery
relaunches and flags). No tier is required by the engine; the tier in use is recorded on the
campaign so a report can state what its recovery guarantee actually was.

### Post-launch provenance verification

Where the job system records the code version a job ran from, the engine reads it back after
launch and asserts it equals the `commit_sha` it intended to submit. A mismatch aborts the
experiment as `invalid` rather than recording a result.

This is cheap and catches the failure that is otherwise undetectable: a launcher bug, or a race
where the branch moved between commit and submission. Without it, an experiment can silently
measure code that is not the code the ledger says it measured — and every downstream conclusion
inherits the error.

### Optional commands

| Command | Purpose |
| --- | --- |
| `progress` | Prints intermediate metrics as JSON. Surfaced in the inspector; enables `kill_criteria` early abort once `cancel` exists |
| `logs` | Prints a log tail. **Required when any status maps to `triage`** (below). Also surfaced in the inspector and given to the analyst |

---

## Repair iteration inside `implement` (D26)

A compile error says nothing about whether the hypothesis was good. Forcing it to surface as a
negative result teaches the proposer that fusing the kernel does not help, when the truth is that
the agent fumbled a template parameter. So the coding agent gets `build` and `test` as **tools
inside its sandbox** and iterates until the change is runnable.

### The line: has the change started producing evidence?

Not "small vs. large" and not "easy vs. hard" — the boundary is whether the code has executed the
intended change at all. Before that, nothing has been learned about the idea. After it, everything
is evidence.

| Agent may iterate — the change does not yet exist in runnable form | Must surface untouched — the change ran and this is what it did |
| --- | --- |
| Compile, syntax, type errors | Existing tests fail |
| Import errors, missing dependency declarations | Numerics drift beyond the declared tolerance |
| Lint and format failures | Training diverges, loss goes NaN |
| Crash on startup in the new code path | The benchmark runs and the metric regresses |
| Patch does not apply to the base commit | Memory or latency guardrail violated |

Retrying anything in the right-hand column is how you get an agent that makes the tests pass by
whatever means are available — precisely what doc 08 exists to prevent.

### Why the loop lives inside the stage, not in the DAG

The workflow is acyclic (lint rule 1), so `implement → build → implement` cannot be a workflow
edge without breaking resumption. Instead the build is a **tool the agent calls**, which is also
how coding agents naturally work. The DAG stays linear and no new machinery is needed.

`verify_build` after `review` is therefore not redundant: it is a **clean-room build of the
committed SHA**, catching the case where the change built in the agent's dirty worktree but does
not build from a fresh checkout — an uncommitted file, a stale artifact, a generated file that was
never added. A `verify_build` failure after the agent reported success is `experiment_failure`
with no further iteration: something is wrong beyond a syntax slip.

### Hypothesis drift — the real risk of iteration

The obvious worry is an agent hacking tests. The subtler and more likely one is this: the agent
cannot get the fusion to compile, and under repair pressure it converges on something that *does*
compile but no longer implements the hypothesis. The engine then spends eight GPU-hours measuring
a near-no-op and records "kernel fusion: no effect."

That is worse than a failed experiment. It is a **false null result**, and it will be fed to the
proposer as evidence and written into the research summary as an established finding.

Three controls:

1. **Fidelity check in `review`** (doc 08). The final diff is compared against the original
   `change_spec`. A diff that no longer implements the stated intent ends the experiment as
   `could_not_implement` *before* any job launches.
2. **Iteration cap** (`max_repair_iterations`, default 3), after which the outcome is
   `could_not_implement`.
3. **Every iteration is recorded** — attempt count, what failed, what changed — so thrash is
   visible in the inspector rather than hidden inside one stage's runtime.

### `could_not_implement` is a distinct outcome

"The agent could not build this" and "this idea does not work" are different facts, and collapsing
them corrupts the search. The first says nothing about the hypothesis; the second is the finding
the campaign exists to produce.

They are therefore separate outcomes, surfaced to the proposer differently: a falsified hypothesis
closes a direction, while `could_not_implement` leaves it open and signals that the idea needs a
more specific `change_spec` or should be broken into smaller steps. Several of these clustering in
one structural family is itself informative — the family may be hard to express in this codebase
rather than unpromising, and marking it saturated would be the wrong call.

---

### Failure classification

The infra-vs-experiment distinction (doc 03) decides both whether to retry and what the proposer
learns, and job status alone often cannot supply it. A status of `preempted` is unambiguous; a
status of `error` or `failed` is not — it covers both a node that died and a change that
diverged. Resolving those requires reading the logs.

Three tiers, cheapest first:

**Tier 1 — status is decisive.** `preempted`, `killed`, quota and scheduling errors map straight
to `infra`. No agent, no cost, no ambiguity. Configured as a status map in the workflow:

```yaml
    status_map:
      preempted: infra
      quota_exceeded: infra
      succeeded: success
      error: triage          # ambiguous — escalate
      failed: triage
```

**Tier 2 — triage agent.** For statuses mapped to `triage`, a `triage` stage fetches the job log
via the `logs` command and classifies. It is a local stage: bounded, cheap, and re-runnable, and
one LLM call against a log tail is negligible next to an 8-hour job.

```yaml
  - key: triage
    kind: local
    handler: autoresearch.agents.triage
    runs_on: stage_failure          # not a DAG node — invoked on failure of an external stage
    timeout: 5m
```

Output is structured and narrow:

```jsonc
{ "class": "infra" | "experiment" | "unknown",
  "confidence": 0.0-1.0,
  "evidence": "verbatim excerpt from the log",
  "summary": "one line, shown to the proposer if class=experiment" }
```

`evidence` must be a **verbatim substring of the log**, checked by the engine. A verdict whose
evidence does not appear in the log is discarded and treated as `unknown` — this is what keeps
triage anchored to what actually happened rather than to what the model found plausible.

The verdict is recorded as `StageFailureTriaged` with its evidence. A wrong verdict either burns
budget on doomed retries or teaches the proposer a false negative result, so it must be auditable
after the fact.

**Tier 3 — the ceiling, which is not optional.** Regardless of what triage says, `infra`
classification is capped at `max_infra_reclassify` attempts (default 3), after which the failure
is reclassified `experiment`. Triage is an optimization on top of this backstop, never a
replacement for it. See doc 08 §3 for why the backstop cannot be removed even if triage proves
highly accurate.

`logs` is therefore **required** whenever any status maps to `triage`, and optional otherwise.

### Poll behaviour

`poll` runs on every controller tick for every in-flight stage, including immediately after a
crash. It must be **cheap, idempotent, and side-effect free**. At D8 concurrency (2–4) and a 60s
interval this is a handful of calls per minute; treat anything expensive as a bug.

Polls are not individually logged — they would drown the transition log. Only *transitions* are
recorded; `last_polled_at` is a column updated in place.

---

## Stage contract (implementation interface)

```python
class Stage(Protocol):
    kind: Literal["local", "external_job"]

    def plan(self, ctx: StageContext) -> PlannedStage:
        """Pure. Resolve inputs, compute inputs_hash. No side effects."""

    def start(self, ctx: StageContext, key: IdempotencyKey) -> Handle:
        """Perform the side effect. MUST stamp `key` onto the external resource.
        MUST be safe to call twice with the same key."""

    def poll(self, ctx: StageContext, handle: Handle) -> StageStatus: ...

    def reattach(self, ctx: StageContext, key: IdempotencyKey) -> Handle | None:
        """Find an already-running effect by key. Required for external_job."""

    def finalize(self, ctx: StageContext, handle: Handle) -> StageOutputs: ...
    def cancel(self, ctx: StageContext, handle: Handle) -> None: ...
```

`CommandExecutor` is the single implementation of this protocol for `external_job`, shelling out
to the four declared commands. Kubernetes and Slurm executors are optional later conveniences
that skip the user's scripts — they are not the foundation.

For `local` stages, `start` runs the handler to completion in a sandboxed subprocess (doc 08),
`reattach` returns `None`, and recovery means re-execution.

---

## Determinism and provenance

Every experiment records: `commit_sha`, `base_commit`, `diff_hash`, `image_digest` (if the launch
script uses one), `dataset_version`, `hardware_class`, `engine_version`, `workflow_version`,
`seed`. Written in `ExperimentCreated` and compared before any result reuse — a cache hit against
a different base commit is not a cache hit.

**Provenance drift detection.** If two experiments in a campaign share a `resolved_config_hash`
but differ in provenance and produce materially different metrics, the campaign's comparability
assumption is broken. Logged loudly; configurable to invalidate affected results.

This is the failure mode that silently poisons long campaigns: a base image is rebuilt under a
mutable tag, and everything before and after becomes incomparable without anyone noticing.
Pinning by digest prevents most of it; the detector catches the rest. At 1–8 hours per experiment
(D7) a campaign runs for days, which is more than enough time for infrastructure to move
underneath it.

---

## Intermediate metrics and early kill

Stages declaring a `progress` command are polled for partial metrics on a slower interval
(default 10× `poll_interval`). The controller evaluates the hypothesis's `kill_criteria` against
them and can abort early with `ExperimentAborted(reason=kill_criteria)`.

At 1–8 hours per experiment this is one of the largest available budget savings — a change that
has clearly regressed by 20 minutes in need not consume another seven hours.

Early-killed experiments are reported to the proposer as *partial evidence*, explicitly labelled
truncated, never as a completed negative result.
