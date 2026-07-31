# 06 — Inner Loop: Workflow Specification and Stage Contract

> **Status:** the stage contract is settled (D4). The workflow authoring surface depends on
> `OPEN-QUESTIONS.md` Q2 (agentic vs. sweep), Q3 (how stages are authored), Q10 (scheduler).

The inner loop is the user-configured workflow for a single experiment, compiled to a durable
state machine. An experiment is complete only when the workflow has been exercised to a terminal
state.

---

## Workflow spec

A DAG of stages, versioned by content hash. Declarative — it describes *what* runs, not control
flow, so the engine can schedule, resume, and re-attach without executing user code to find out
what happens next.

```yaml
name: latency-opt-workflow
version: 3                       # hashed into workflow_version

inputs:                          # schema hypothesis.parameters must satisfy
  kernel_variant: { type: enum, values: [baseline, fused, tiled] }
  block_size:     { type: int, min: 32, max: 512 }

stages:
  - key: codegen
    kind: in_process             # cheap, re-runnable
    handler: handlers.codegen
    timeout: 10m
    retries: { max: 2, on: [infra] }

  - key: build
    kind: external_job
    needs: [codegen]
    executor: k8s
    image: "{{ provenance.image_digest }}"
    command: ["make", "build"]
    resources: { cpu: 8, memory: 32Gi }
    timeout: 30m
    retries: { max: 3, on: [infra] }

  - key: benchmark
    kind: external_job
    needs: [build]
    executor: k8s
    resources: { gpu: 1, gpu_class: h100 }
    timeout: 2h
    checkpoint: { every: 10m }
    emits_intermediate_metrics: true     # enables kill_criteria (doc 05)
    outputs:
      metrics: { from: "artifacts/metrics.json", schema: project.metric_registry }

  - key: analyze
    kind: in_process
    needs: [benchmark]
    handler: handlers.analyze            # agent writes the interpretation

terminal: [analyze]

on_failure:
  - stage: build
    class: experiment
    action: record_and_stop        # a build break is a real negative result, not an infra retry
```

### Rules the spec must enforce at lint time

1. **DAG, no cycles.** Loops are expressed as retries or as the outer loop, never as workflow
   edges — otherwise resumption has no well-defined position.
2. **`in_process` stages must be cheap.** A declared `timeout` above the in-process ceiling
   (default 10m) is a spec error. This is the rule that makes the durability guarantee real
   (`04-durability.md` §3), so it is enforced, not advised.
3. **Every stage key is stable across versions** where semantics are unchanged — resumption and
   cross-version comparison both key on it.
4. **Exactly one metrics-producing stage** must map into the project metric registry, and its
   output must typecheck against it. An experiment that cannot produce comparable metrics is
   not an experiment.
5. **`retries.on` may only list `infra`.** Retrying an `experiment` failure re-runs a
   deterministic failure and burns budget for no information.

---

## Stage contract

Both kinds implement the same interface; only the mechanics differ (D4: both, per stage kind).

```python
class Stage(Protocol):
    kind: Literal["in_process", "external_job"]

    def plan(self, ctx: StageContext) -> PlannedStage:
        """Pure. Resolve inputs, compute inputs_hash. No side effects."""

    def start(self, ctx: StageContext, key: IdempotencyKey) -> Handle:
        """Perform the side effect. MUST stamp `key` onto the external resource.
        MUST be safe to call twice with the same key (second call re-attaches)."""

    def poll(self, ctx: StageContext, handle: Handle) -> StageStatus:
        """Cheap, idempotent, side-effect free. Returns RUNNING | COMPLETED | FAILED
        with a failure class, plus optional intermediate metrics."""

    def reattach(self, ctx: StageContext, key: IdempotencyKey) -> Handle | None:
        """Find an already-running effect by key. Required for external_job."""

    def finalize(self, ctx: StageContext, handle: Handle) -> StageOutputs:
        """Collect outputs, content-address artifacts, report cost."""

    def cancel(self, ctx: StageContext, handle: Handle) -> None: ...
```

For `in_process` stages, `start` runs the handler to completion in a worker thread/process,
`reattach` returns `None` (nothing to rejoin), and recovery means re-execution.

`poll` being side-effect free matters more than it looks: it runs on every controller tick for
every in-flight stage, including immediately after a crash, and any side effect there would fire
repeatedly and unpredictably.

### Executors

An executor implements `start`/`poll`/`reattach`/`cancel` for one backend. The requirement from
`04-durability.md`: it must support client-supplied idempotency tokens, queryable labels, or
deterministic naming. Anything else cannot guarantee exactly-once launch.

| Executor | Key mechanism |
| --- | --- |
| `local_process` | PID file + key in a state dir; weakest, dev only |
| `k8s` | Job name derived from key, or key as a label; `reattach` = get/list by label |
| `slurm` | Job name/comment carries the key; `reattach` = `squeue` filter |
| `cloud_batch` | Native idempotency token |
| `http_service` | Idempotency-Key header; the service must honour it |

**OPEN (Q3, Q10):** which executors are needed for v1, and whether the engine schedules directly
or delegates to an existing platform.

---

## Determinism and provenance

Every experiment pins `git_sha`, `image_digest`, `dataset_version`, `hardware_class`,
`engine_version`, `workflow_version`, and `seed`. Recorded in `ExperimentCreated`, and compared
before any result reuse — a cache hit against a different image digest is not a cache hit.

**Provenance drift detection.** If two experiments in a campaign share a `resolved_config_hash`
but differ in provenance and produce materially different metrics, the campaign's comparability
assumption is broken. This is logged loudly and can be configured to invalidate affected results.
It is the failure mode that silently poisons long campaigns — a base image gets rebuilt under a
mutable tag, and everything before and after becomes incomparable without anyone noticing. Pinning
by digest rather than tag prevents most of it; the detector catches the rest.

---

## Intermediate metrics and early kill

Stages declaring `emits_intermediate_metrics` stream partial metrics through `StageProgress`
(sampled, rate-limited — these are high volume and must not dominate the log). The controller
evaluates the hypothesis's `kill_criteria` against them and can abort early with
`ExperimentAborted(reason=kill_criteria)`. On a long-running workload this is one of the largest
available budget savings, and it is only possible if the workflow surfaces progress — which is
why it belongs in the workflow spec rather than being bolted on later.

Early-killed experiments are reported to the proposer as *partial evidence*, explicitly labelled
as truncated, never as a completed negative result.
