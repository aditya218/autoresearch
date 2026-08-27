# Example: a synthetic MLE task on Slurm

A complete, runnable campaign. The research task is small and synthetic, but
everything around it is real: trials edit real model code, the expensive
training run is submitted to Slurm with `sbatch`, polled with `sacct`, and
scored by a harness the trial cannot modify.

## The task

Fit a synthetic 2-D regression problem — `sin(2.5·x₁) + 0.5·x₂² − 0.3·x₁·x₂`
plus noise — with a small MLP written in pure Python (no dependencies). The
metric is **validation RMSE, minimized**, on a fixed train/val split.

There is genuine headroom, which is what makes it a useful demo:

| Configuration | val_rmse |
|---|---|
| Baseline (`hidden_size: 8`, `lr: 0.02`, no momentum, 60 epochs) | 0.201 |
| Momentum 0.9 + 200 epochs | 0.090 |
| Wider network, tuned lr | 0.104 |
| Learning rate 0.9 | diverges |

That last row matters: a diverging run is reported as a **failed idea**, not
an infrastructure error, so it counts against the trial budget the way a real
negative result should.

## What a trial may and may not touch

```
base_code/          # the workspace: a trial edits these freely
  config.json         hyperparameters
  model.py            architecture, initialisation, update rule

eval/               # NOT in the workspace: a trial cannot rewrite its own score
  data.py             the fixed dataset and split
  train_eval.py       trains the workspace model, writes metrics.json
```

This is the metric-integrity rule made concrete: `val_rmse` is bound to the
deterministic `train` phase in `campaign.yaml`, so a number reported by any
agentic phase is ignored.

## The workflow

```
implement (agentic)  →  smoke_test (gate, local)  →  train (Slurm)  →  analyze (agentic)
```

Escalation by cost: an idea that cannot train for five local epochs never
reaches the cluster. `train` declares a repair skill, so a job stuck in a
state the engine has no rule for gets diagnosed rather than abandoned.

## Running it

**On a real cluster** — the scripts issue ordinary Slurm commands
(`sbatch --parsable`, `sacct`, `squeue`), so nothing needs changing:

```bash
autoresearch validate examples/slurm_mle/campaign.yaml
autoresearch run examples/slurm_mle/campaign.yaml \
    --campaign-dir ~/campaigns/mle --harness sdk --ideate
```

Set `SLURM_PARTITION`, or edit `params` under the `train` phase, to match
your cluster.

**Without a cluster** — `fake_slurm/` provides `sbatch`/`sacct`/`squeue`
shims that run the job locally in the background and report the same states.
The project's scripts cannot tell the difference:

```bash
export PATH="$PWD/examples/slurm_mle/fake_slurm:$PWD/examples/slurm_mle:$PATH"
export FAKE_SLURM_STATE=/tmp/fake-slurm

autoresearch run examples/slurm_mle/campaign.yaml \
    --campaign-dir /tmp/mle-campaign --harness fake_agent --ideate \
    --poll-interval 0.3
```

`fake_agent` is a scripted stand-in that applies a fixed hyperparameter
search, so the example runs deterministically without an LLM.

**With a real agent**, use the in-process Claude Agent SDK harness:

```bash
autoresearch run examples/slurm_mle/campaign.yaml \
    --campaign-dir /tmp/mle-campaign --harness sdk --ideate \
    --effort high --max-turns 40 --max-budget-usd 5
```

or any harness CLI (`--harness 'claude -p'`). Every invocation is bounded in
turns, spend, and wall-clock, and the agent can reach only the trial's
workspace, its own phase directory, and the read-only project directory.

## Skills

`skills/` is where this project teaches agents what it knows. The engine
resolves them itself and inlines them into the prompt, so the same text
reaches the SDK, a CLI, or a scripted stand-in unchanged:

| Skill | Used by | Teaches |
|---|---|---|
| `propose-mle-idea` | ideation | what is changeable, one hypothesis at a time, that lr ≥ 0.5 diverges here |
| `implement-mle-idea` | `implement` | the entry points the eval harness calls, and to change only what the idea asks |
| `analyze-mle-results` | `analyze` | read the training curve, compare against parent and baseline, don't call noise a win |
| `repair-slurm-job` | repair | the real Slurm failure shapes, and when to escalate rather than guess |

These are worth reading even if you use a different cluster — they are the
concrete form of "projects supply capability, the engine supplies contracts."

A representative run:

```
campaign budget_reached
  T000: completed  val_rmse=0.20094      <- baseline
  T003: completed  val_rmse=0.19205
  T004: completed  val_rmse=0.13671
  T005: completed  val_rmse=0.08966      <- momentum 0.9, 200 epochs
```

## Exercising the failure paths

```bash
# a job stuck in a state the engine has no rule for -> repair agent
FAKE_SLURM_STUCK=<job_id> ./poll train --job-id <job_id>

# an idea that diverges -> the gate stops it before the cluster job
./run smoke_test --workspace <ws-with-lr-0.9> --out /tmp/out --epochs 5
```

## Adapting it to your own cluster

Only the four scripts are cluster-specific. Keep the contract:

| Script | Must do |
|---|---|
| `launch` | submit the job, print **only** the job id; attach `--tag` as the job name |
| `poll` | print `running`, `done`, or `failed` — anything else is treated as a repair situation |
| `collect` | write a valid `result.json` from the job's output |
| `find` | print job ids matching a tag, so an ambiguous launch is recoverable |
