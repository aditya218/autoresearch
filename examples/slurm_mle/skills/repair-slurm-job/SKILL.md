# Repairing a Slurm job

The engine reached a situation it has no rule for and is asking you what to
do. Investigate, then recommend exactly one action. You do not act — the
engine performs whatever you recommend, so that every change is recorded.

## Look at these first

- `metrics.json` in the phase directory — **the deciding evidence.** If it
  exists and is complete, the work finished no matter what Slurm says.
- `slurm-<jobid>.out` / `.err` — the job's own output
- `job.sbatch` — what was actually submitted
- Re-run the poll: `sacct -j <job_id> --format=State,ExitCode,Elapsed`

## The situations you will actually see

**Accounting lag.** `sacct` says `RUNNING` or `UNKNOWN`, but `metrics.json`
is present and complete. Slurm's accounting has not caught up. → `collect`.

**Preemption or node failure.** `NODE_FAIL`, `PREEMPTED`, or a log ending
abruptly with no Python traceback. The idea was never really tested. →
`relaunch` (first time), or `fail_infra` if it has already been relaunched.

**Out of memory.** `OUT_OF_MEMORY`, or the log shows the kernel killing the
process. Decide by cause: if the trial's own change made the model far
bigger, that is a property of the idea → `fail_idea`. If the model is the
same size as trials that ran fine, it is the cluster → `fail_infra`.

**Timeout.** `TIMEOUT` with partial output. If the idea asked for far more
epochs than the time limit allows, that is the idea's fault → `fail_idea`.
Otherwise → `relaunch`.

**A Python traceback in the log.** The trial's code is broken. That is a real
result about the idea → `fail_idea`, quoting the exception in your diagnosis.

**Still healthy, just slow.** A long-running job making progress, with recent
output. → `wait`.

## Choosing

- `collect` — results exist and are usable despite the state
- `relaunch` — transient; running it again should work
- `wait` — healthy, keep polling
- `fail_infra` — infrastructure's fault; does not count against the budget
- `fail_idea` — the idea's fault; counts as a real evaluation
- `escalate` — you cannot tell

**Prefer `escalate` to a guess.** A wrong `fail_idea` records a false
negative result in the ledger and may steer the whole campaign away from a
good direction; a wrong `relaunch` burns an allocation. Escalating costs one
person one look.

Never recommend `collect` unless you have confirmed `metrics.json` is
actually there and parses.

## Output

Write `repair.json` in the phase directory:

```json
{"action": "collect",
 "diagnosis": "sacct reports RUNNING but metrics.json is complete with val_rmse 0.14; job log ends with the normal completion line"}
```

Cite what you looked at in `diagnosis`. Somebody reviewing this later needs
to know whether to trust the call — and these diagnoses are what get promoted
into this skill as the campaign learns your cluster's failure modes.
