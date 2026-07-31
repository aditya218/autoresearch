# 01 — Data Model

## Entity overview

```
project ──1:N──> campaign ──1:N──> run            (execution sessions, leased)
                     │
                     ├──1:N──> hypothesis ──1:N──> experiment ──1:N──> replicate
                     │                                  │
                     │                                  └──1:N──> stage_execution
                     └──1:1──> campaign_config (immutable snapshot)
```

Key cardinality decisions, and why:

- **hypothesis → experiment is 1:N, not 1:1.** One idea may need an ablation set, a small
  parameter sweep, or a re-run at higher fidelity. Forcing 1:1 makes replication impossible.
- **experiment → replicate is 1:N.** A replicate is one execution of the workflow at a fixed
  seed. Experiment-level metrics are *aggregations* over replicates with a dispersion estimate.
  An experiment with `replicates = 1` is the degenerate common case.
- **run does not own experiments.** An experiment carries `created_by_run_id` for provenance,
  and each `stage_execution` records the `run_id` that drove that attempt. A crash-and-resume
  therefore produces a new run without orphaning anything.

> **Naming note.** The word *trial* is deliberately avoided: in HPO tooling it means "one
> config evaluation" (our `experiment`), and in the original requirements it was used to mean
> the same. The per-seed unit is called `replicate` throughout.

---

## project

The long-lived thing being optimized. Owns the metric vocabulary; campaigns inherit it.

| Field | Type | Notes |
| --- | --- | --- |
| `project_id` | ULID | PK |
| `name` | text | e.g. `v4-model-latency` |
| `description` | text | Human framing of the optimization target |
| `metric_registry` | jsonb | Typed metric definitions — see `07-objectives-and-validity.md` |
| `default_workflow_ref` | text? | Optional default inner-loop workflow for new campaigns |
| `created_at` / `created_by` | timestamptz / text | |

`metric_registry` is at project level so results are comparable **across** campaigns. A
campaign may narrow it (pick a primary, add constraints) but may not redefine a metric's
meaning, unit, or direction.

---

## campaign

One specific attempt to optimize a project. All experiments within a campaign are mutually
visible to the proposer.

| Field | Type | Notes |
| --- | --- | --- |
| `campaign_id` | ULID | PK |
| `project_id` | ULID | FK |
| `parent_campaign_id` | ULID? | Set when forked from another campaign |
| `fork_reason` | text? | e.g. `config_edit`, `branch_exploration` |
| `config` | jsonb | **Immutable** snapshot — see below |
| `config_hash` | text | sha256 of canonicalized `config` |
| `status` | enum | See `03-lifecycle.md` |
| `stop_reason` | enum? | `budget_exhausted` \| `converged` \| `target_reached` \| `manual` \| `fatal_error` |
| `seed_context` | jsonb? | Human-provided priors, prior art, constraints given to the proposer |
| `created_at` / `ended_at` | timestamptz | |

### campaign.config (immutable)

```jsonc
{
  "objective":  { /* primary metric, direction, constraints — doc 07 */ },
  "workflow":   { /* inner-loop stage DAG — doc 06 */ },
  "workflow_version": "sha256:...",
  "proposer":   { /* model, strategy, context policy — doc 05 */ },
  "budget":     { "max_experiments": 200, "max_cost_usd": 5000,
                  "max_wallclock_hours": 72, "max_concurrent_experiments": 4 },
  "stopping":   { "no_improvement_experiments": 40, "target_metric_value": 0.93 },
  "replication":{ "default_replicates": 1, "confirmation_replicates": 5 },
  "provenance_pins": { "base_commit": "...", "image_digest": "...", "dataset_version": "..." },
  "safety":     { /* protected paths, sandbox policy, approval gates — doc 08 */ }
}
```

### Mutability rule (D18)

The line is drawn at **measurement and execution** versus **search policy and budget**.

| Frozen once `ACTIVE` | Editable in place, recorded as events |
| --- | --- |
| `objective`, metric selection, guardrail thresholds | `budget` (increases, and decreases above spend) |
| `workflow` and `workflow_version` | `max_concurrent_experiments` |
| `replication` policy | `proposer` model and prompt settings |
| `provenance_pins`, including `base_commit` | `stopping` criteria |
| `safety.protected_paths` | `safety.approval_gates` |

Editing a frozen field creates a child campaign with `parent_campaign_id` set, which inherits the
parent's ledger read-only (D13 fork carve-out) so it does not rediscover what the parent already
established. Freezing the left column is what makes "is experiment A better than experiment B" a
well-formed question; the right column changes only how the *next* idea is chosen, which no past
result depends on.

---

## run

An execution session of the campaign controller. Holds an exclusive lease; see `04-durability.md`.

| Field | Type | Notes |
| --- | --- | --- |
| `run_id` | ULID | PK |
| `campaign_id` | ULID | FK |
| `fencing_token` | bigint | Monotonic per campaign; stamped on every event this run appends |
| `worker_identity` | text | host/pod/process identity, for debugging |
| `engine_version` | text | Build of the engine itself |
| `status` | enum | `ACTIVE` \| `DRAINING` \| `ENDED` |
| `end_reason` | enum? | `clean_shutdown` \| `lease_lost` \| `crashed` \| `campaign_stopped` |
| `started_at` / `heartbeat_at` / `ended_at` | timestamptz | |

A run whose `heartbeat_at` has gone stale is presumed dead; its lease becomes acquirable and
its in-flight work is reconciled by the next run (not discarded).

---

## hypothesis

One idea to try. Generated by the proposer from the ledger, or injected by a human.

| Field | Type | Notes |
| --- | --- | --- |
| `hypothesis_id` | ULID | PK |
| `campaign_id` | ULID | FK |
| `created_by_run_id` | ULID? | Null for human-authored |
| `origin` | enum | `proposer` \| `human` \| `seed` \| `auto_replication` \| `auto_repair` |
| `actor` | text? | Human who authored it, when `origin = human` (D20 attribution) |
| `statement` | text | Natural-language claim: "X will improve Y because Z" |
| `rationale` | text | Reasoning, with explicit references to prior experiment_ids |
| `change_spec` | jsonb | Instruction to the coding agent: intent, files of interest, constraints, acceptance criteria (D5) |
| `structural_family` | text | Kind of change, e.g. `kernel-fusion`. The mode-collapse instrument (doc 05) |
| `parameters` | jsonb | Typed launch parameters, separate from the code change |
| `predicted_effect` | jsonb? | Proposer's forecast: metric, direction, magnitude, confidence |
| `predicted_cost` | jsonb? | Estimated cost/duration; used for admission control |
| `derived_from` | jsonb | `{ "experiment_ids": [...], "hypothesis_ids": [...] }` — lineage |
| `dedup_fingerprint` | text | Canonicalized-parameter hash, for exact dedup |
| `semantic_embedding` | vector? | For near-duplicate detection |
| `priority` | float | Queue ordering score |
| `state` | enum | See `03-lifecycle.md` |
| `state_reason` | text? | Why rejected / superseded / expired |
| `claim` | jsonb? | `{ run_id, expires_at }` when claimed by a run |
| `proposed_at_experiment_count` | int | Ledger depth at proposal time — drives staleness |

`proposed_at_experiment_count` exists because ideas rot. An idea proposed when 12 experiments
were complete may be answered, invalidated, or made obsolete by experiment 40. Staleness policy
lives in `05-outer-loop.md`.

---

## experiment

The materialization of a hypothesis: a concrete, executable, budgeted unit of work. Completing
an experiment means exercising the user-configured workflow to a terminal state.

| Field | Type | Notes |
| --- | --- | --- |
| `experiment_id` | ULID | PK |
| `campaign_id` / `hypothesis_id` | ULID | FK |
| `created_by_run_id` | ULID | Provenance only — never ownership |
| `variant_label` | text? | Distinguishes siblings from one hypothesis (`ablation:no-cache`) |
| `role` | enum | `primary` \| `ablation` \| `replication` \| `confirmation` \| `baseline` |
| `workflow_version` | text | Hash of the workflow spec actually executed |
| `branch` | text | `autoresearch/{campaign_id}/{experiment_id}` |
| `base_commit` | text | Pinned base the branch was cut from — identical for every experiment in the campaign |
| `commit_sha` | text? | The agent's commit. Null until the `implement` stage completes |
| `diff_hash` | text? | sha256 of the normalized diff. **Exact** dedup and cache key (doc 05) |
| `resolved_config` | jsonb | Launch parameters after applying `hypothesis.parameters` |
| `resolved_config_hash` | text | `H(commit_sha, resolved_config)` — cache lookup |
| `provenance` | jsonb | base_commit, image digest, dataset version, hardware class, engine version |
| `state` | enum | See `03-lifecycle.md` |
| `outcome` | enum? | `success` \| `experiment_failure` \| `infra_failure` \| `aborted` \| `invalidated` |
| `outcome_detail` | text? | |
| `metrics` | jsonb? | Aggregated over replicates: `{value, stddev, n, ci_low, ci_high}` per metric |
| `guardrail_violations` | jsonb? | Which guardrails failed, with values |
| `cost` | jsonb | `{ usd, gpu_seconds, wallclock_seconds }` accumulated |
| `artifacts` | jsonb | Filesystem paths under the campaign directory, with integrity hashes (D16) |
| `analysis` | text? | Agent-written interpretation, fed back to the proposer |
| `started_at` / `ended_at` | timestamptz | |

**`diff_hash` enables the single biggest efficiency win.** Once the coding agent has produced its
change, check whether an experiment with the same diff and the same provenance already succeeded.
If so, reuse the result rather than paying for another 1–8 hour job. Per D13 the lookup is scoped
to **this campaign**, plus the parent's ledger when this campaign is a fork.

This matters more with a pure-LLM proposer (D6) than it would with a sampler: distinct-sounding
hypotheses regularly compile down to the same change.

---

## replicate

One execution of the workflow at a fixed seed.

| Field | Type | Notes |
| --- | --- | --- |
| `replicate_id` | ULID | PK |
| `experiment_id` | ULID | FK |
| `seed` | bigint | |
| `state` / `outcome` | enum | Mirrors experiment |
| `metrics` | jsonb? | Raw per-seed metrics |
| `cost` | jsonb | |

Experiment-level `metrics` are computed from replicates by the aggregation rule in the objective
spec (default: mean + sample stddev + t-interval).

---

## stage_execution

One attempt at one stage of the inner loop, for one replicate.

| Field | Type | Notes |
| --- | --- | --- |
| `stage_execution_id` | ULID | PK |
| `replicate_id` | ULID | FK |
| `stage_key` | text | Stable key from the workflow spec |
| `attempt` | int | Retry counter |
| `run_id` | ULID | The run that drove *this attempt* |
| `kind` | enum | `local` \| `external_job` |
| `idempotency_key` | text | Unique; stamped onto the external job as a tag. See `04-durability.md` |
| `external_handle` | jsonb? | `{ job_id }` as printed by the user's `launch` command |
| `state` | enum | See `03-lifecycle.md` |
| `inputs_hash` / `outputs` | text / jsonb | Outputs are filesystem paths under the stage's directory |
| `cost` | jsonb | |
| `started_at` / `ended_at` / `last_polled_at` | timestamptz | `last_polled_at` is updated in place — polls are not events |

---

## Storage layout (D16)

Postgres holds anything queried, filtered, or joined on. The distributed filesystem holds
anything large or opaque. Paths are **derived from IDs**, never stored as free-form strings, so
recovery locates artifacts by computation rather than by search.

```
{root}/{project_id}/{campaign_id}/
  config.json                                        # immutable snapshot, human-readable
  summary/{version}.md                               # research summary history
  experiments/{experiment_id}/
    diff.patch                                       # the agent's change — primary artifact
    replicates/{replicate_id}/
      metrics.json
      stages/{stage_key}/{attempt}/
        launch.json, stdout.log, stderr.log, outputs/
```

Three invariants:

1. **The filesystem is never authoritative for state.** Recovery reads Postgres, never a
   directory listing. A partially-written directory or a stale distributed-FS listing must not be
   able to confuse the controller.
2. **Write, then record.** Write to a temp path, rename atomically, *then* append the event
   carrying the ref. The reverse order produces refs to files that do not exist.
3. **Each stage attempt owns its own directory**, so concurrent writers never contend by
   construction — including a re-attached attempt racing a zombie's leftover process.

---

## Central registry

A single place listing all projects, campaigns, and runs, with live rollups. Implemented as a
projection (`04-durability.md`), not a source of truth:

```sql
CREATE VIEW registry_campaigns AS
SELECT c.campaign_id, c.project_id, p.name AS project_name, c.status, c.stop_reason,
       (SELECT count(*) FROM experiment e WHERE e.campaign_id = c.campaign_id) AS experiments,
       (SELECT count(*) FROM experiment e WHERE e.campaign_id = c.campaign_id
          AND e.state = 'RUNNING')                                            AS in_flight,
       (SELECT count(*) FROM hypothesis h WHERE h.campaign_id = c.campaign_id
          AND h.state = 'QUEUED')                                             AS pending_ideas,
       (SELECT run_id FROM run r WHERE r.campaign_id = c.campaign_id
          AND r.status = 'ACTIVE' ORDER BY started_at DESC LIMIT 1)           AS active_run,
       c.created_at, c.ended_at
FROM campaign c JOIN project p USING (project_id);
```

## Indices that matter

```sql
CREATE UNIQUE INDEX ON hypothesis (campaign_id, dedup_fingerprint);
CREATE INDEX        ON hypothesis (campaign_id, state, priority DESC);
CREATE INDEX        ON experiment (campaign_id, state);
CREATE INDEX        ON experiment (campaign_id, resolved_config_hash);
CREATE INDEX        ON experiment (campaign_id, diff_hash);   -- exact dedup / result cache
CREATE UNIQUE INDEX ON stage_execution (idempotency_key);
CREATE INDEX        ON stage_execution (state, last_polled_at)
                       WHERE state IN ('LAUNCHED','RUNNING');   -- the poller's work queue
```
