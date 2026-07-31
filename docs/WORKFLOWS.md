# Configuring a campaign

A user writes two things:

1. **`campaign.yaml`** — the objective, the budget, and the workflow: what stages run, in what
   order, and what commands they invoke.
2. **Your scripts** — `launch`, `poll`, and optionally `find` and `logs`. The engine never learns
   what your job does; it submits a command, polls an id, and reads a metrics file.

```bash
autoresearch campaign-create --project <id> --config campaign.yaml   # validates, stays DRAFT
autoresearch campaign-start  <campaign_id>                           # freezes config, ACTIVE
autoresearch run-start       --campaign <campaign_id>                # drives it; also resumes it
```

Validation happens at `campaign-create`, before anything is stored. A bad workflow is a startup
error, not a 3am surprise.

---

## The file

```yaml
objective:
  primary: { metric: p50_latency_ms, direction: minimize }
  constraints:
    - { metric: eval_quality, op: ">=", value: 0.90, kind: guardrail }

budget:
  max_experiments: 6
  max_concurrent_experiments: 2

replication:
  base_seed: 1000

provenance_pins:
  base_commit: "4a91c02"

workflow:
  name: prototype-latency
  version: 1

  stages:
    - key: prepare                 # cheap, runs on the controller host
      kind: local
      timeout: 2m
      command: ["bash", "-c", "./scripts/prepare.sh"]

    - key: train                   # the expensive part, runs on your scheduler
      kind: external_job
      needs: [prepare]
      launch: "./scripts/launch.sh"
      poll:   "./scripts/status.sh {{ job_id }}"
      find:   "./scripts/find.sh"
      logs:   "./scripts/logs.sh {{ job_id }}"
      timeout: 8h
      poll_interval: 60s
      max_infra_retries: 3
      status_map:
        PREEMPTED: infra
        DIVERGED:  experiment
      outputs:
        metrics: "{{ artifact_dir }}/metrics.json"

    - key: collect
      kind: local
      needs: [train]
      timeout: 2m
      command: ["bash", "-c", "./scripts/collect.sh"]

  terminal: [collect]
```

## Stage fields

| Field | Applies to | Meaning |
| --- | --- | --- |
| `key` | both | Stable identifier. Resumption and retry counting key on it — do not rename it casually |
| `kind` | both | `local` or `external_job` |
| `needs` | both | Dependencies. The graph must be acyclic |
| `timeout` | both | `30s`, `10m`, `8h` |
| `command` | local | argv list, run in a subprocess |
| `launch` | external | Submits the job. Must print the job id on the last line of stdout |
| `poll` | external | Prints one status word |
| `find` | external | Optional. Prints the job id for `$AUTORESEARCH_IDEM_KEY`, or nothing |
| `logs` | external | Optional. Prints a log tail |
| `poll_interval` | external | How often to poll. At 8-hour jobs, 60s is plenty |
| `max_infra_retries` | external | Ceiling on infra retries before the failure is called an experiment result |
| `failure_class` | both | Force every failure of this stage to `infra` or `experiment` |
| `status_map` | external | Your job system's vocabulary → the engine's. See below |
| `outputs.metrics` | one stage | Path to the JSON your job writes. **Exactly one stage must declare this** |

### Which kind should a stage be?

`local` if the work is cheap enough to redo — a controller crash re-executes it from scratch.
`external_job` for anything expensive, because the engine re-attaches to a running job rather than
relaunching it.

This is enforced, not advised: a `local` stage declaring a timeout over 20 minutes is a spec
error. It is the rule that makes the durability guarantee real rather than aspirational.

---

## Template variables

Substituted into command strings:

| Variable | Where |
| --- | --- |
| `{{ job_id }}` | `poll`, `logs` |
| `{{ artifact_dir }}` | `outputs.*` |

## Environment given to your scripts

| Variable | Given to | Use |
| --- | --- | --- |
| `AUTORESEARCH_ARTIFACT_DIR` | all | Where to write metrics and artifacts for this attempt |
| `AUTORESEARCH_IDEM_KEY` | `launch`, `find` | `ar-{campaign}-{experiment}-{stage}-{attempt}` |
| `AUTORESEARCH_RECEIPT` | `launch` | Write the job id here **before exiting** — see below |
| `AUTORESEARCH_JOB_ID` | `poll`, `logs` | Also passed as `{{ job_id }}` |
| `AUTORESEARCH_CAMPAIGN` / `_EXPERIMENT` / `_REPLICATE` / `_STAGE` / `_SEED` | all | Identity and seed |

### The one thing your launcher should do

```bash
# ... submit the job, get $XID ...
[ -n "${AUTORESEARCH_RECEIPT:-}" ] && printf '%s' "$XID" > "$AUTORESEARCH_RECEIPT"
echo "$XID"
```

One line. It closes the window where the controller dies after your job was submitted but before
the id was recorded — without it, recovery cannot tell whether a job is running, and either
relaunches (two jobs, one unwatched) or gives up. `find` closes the window completely if you have
a way to look jobs up by tag; the receipt covers most of it with no lookup tool at all.

---

## `status_map`

Maps your job system's status words to what they mean to the engine. Unlisted statuses fall back
to sensible defaults (`SUCCEEDED`, `RUNNING`, `PREEMPTED`, `FAILED`, …).

| Meaning | Effect |
| --- | --- |
| `success` | Stage completed |
| `running` | Still going |
| `infra` | Failed, **not** a research result — retry, up to `max_infra_retries` |
| `experiment` | Failed, and that **is** the result — do not retry, report it |
| `triage` | Ambiguous — falls through to the retry ceiling |

This distinction is the most valuable thing your scripts can tell the engine. A preempted node
retried as infra costs nothing; the same failure recorded as a research result teaches the
proposer that a good idea does not work. If your tool can distinguish them, map them here.

---

## What the linter rejects

| Rule | Why |
| --- | --- |
| Cycles in `needs` | Resumption needs a well-defined position |
| `local` stage over 20m (60m for `implement`) | A crash re-executes it; expensive work must be external |
| `local` stage with no `command` | Nothing to run |
| `external_job` without `launch` and `poll` | Cannot submit or observe |
| Zero or two stages declaring `outputs.metrics` | An experiment that cannot produce comparable metrics is not an experiment |
| Unknown `needs` target, duplicate `key`, undefined `terminal` | Typos, caught at create time |

---

## Not yet wired up

Honest gaps in the prototype, so the config does not promise more than it does:

- **`objective` is parsed but not evaluated.** Metrics are collected and stored; nothing yet
  checks the guardrail or ranks a leaderboard.
- **`replication` runs one replicate.** `base_seed` is used; multiple seeds are not.
- **`budget` enforces `max_experiments` and `max_concurrent_experiments` only** — no cost or
  wall-clock ceiling.
- **`provenance_pins` is recorded, not verified.** Nothing checks the job ran the pinned commit.
