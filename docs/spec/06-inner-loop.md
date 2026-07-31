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
    timeout: 20m
    retries: { max: 1, on: [infra] }

  - key: review
    kind: local
    handler: autoresearch.safety.diff_review       # protected paths, secrets, gaming — doc 08
    needs: [implement]
    timeout: 5m
    on_reject: abort_experiment

  - key: build
    kind: local
    sandbox: subprocess                            # agent-authored build scripts run here
    command: ["make", "build"]
    needs: [review]
    timeout: 10m
    failure_class: experiment                      # a build break is a result, not an infra fault

  - key: train
    kind: external_job
    needs: [build]
    executor: command
    launch:  "./scripts/launch.sh --commit {{ commit_sha }} --config {{ config_path }}"
    poll:    "./scripts/status.sh {{ job_id }}"
    cancel:  "./scripts/cancel.sh {{ job_id }}"
    find:    "./scripts/find.sh --tag {{ idem_key }}"
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
3. **Every `external_job` must declare all four commands.** `launch`, `poll`, `cancel`, `find`.
   Missing `find` is a spec error, not a warning — without it, exactly-once launch is impossible
   (doc 04) and a crash in the launch window leaks a running job.
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
| `cancel` | `job_id` | — | 0 if cancelled or already terminal |
| `find` | `AUTORESEARCH_IDEM_KEY` | the `job_id` if a job carries this tag, nothing otherwise | 0 either way |

**The launcher must tag the submitted job with `AUTORESEARCH_IDEM_KEY`** — as a label, a job
name, or a comment field, whatever the scheduler supports — so that `find` can recover it. This
is the one requirement the engine places on your infrastructure, and everything in doc 04 §2
depends on it.

### Optional commands

| Command | Purpose |
| --- | --- |
| `progress` | Prints intermediate metrics as JSON; enables `kill_criteria` early abort |
| `logs` | Prints a log tail, surfaced in the inspector and given to the analyst agent on failure |

### Failure classification

`poll` returning `FAILED` is ambiguous — the engine cannot tell a diverged training run from a
preempted node. Resolution, in order:

1. If the workflow declares `failure_class` for the stage, use it.
2. If `poll` prints a JSON object with a `class` field (`infra` | `experiment`), trust it. This
   is the recommended path — your scripts know the difference and the engine does not.
3. Otherwise classify as `infra` and retry, up to `max_infra_reclassify` attempts (default 3),
   after which reclassify as `experiment`. Without that ceiling, a genuinely broken change
   retries forever.

### Poll behaviour

`poll` runs on every controller tick for every in-flight stage, including immediately after a
crash. It must be **cheap, idempotent, and side-effect free**. At D8 concurrency (2–4) and a 60s
interval this is a handful of calls per minute; treat anything expensive as a bug.

Polls are not individually logged — they would dominate the ledger. Only *transitions* produce
events. `last_polled_at` is a projection column, updated in place.

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
