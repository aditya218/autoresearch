# Prototype

Build-order steps 1–3 from [`docs/spec/09-interfaces.md`](docs/spec/09-interfaces.md): the store,
the control loop with recovery, and the command executor. **No agents.** Hypotheses are seeded
from a file, standing in for the proposer.

The goal is narrow and deliberate: prove that a campaign drives trials to completion and survives
being killed at any point. Everything uncertain about this system — whether the outer loop
converges, whether the proposer stays diverse — sits on top of this and is only worth building
once this is solid.

## Run it

```bash
./scripts/setup-db.sh          # local Postgres + schema, idempotent
./scripts/demo.sh              # 4 experiments, 2 concurrent, to completion

export PYTHONPATH=$PWD
python3 -m pytest tests/ -q    # 20 tests, including the crash suite
```

## What works

| Capability | Where |
| --- | --- |
| Campaign / experiment / replicate / stage state machines | `domain/states.py` |
| CAS + fencing + transition log, one transaction, one chokepoint | `store/transitions.py` |
| Campaign lease with fencing tokens; second run refused | `control/lease.py` |
| Startup recovery: re-attach, requeue claims, resume aggregation | `control/engine.py` |
| Local stages (subprocess) and external jobs (user commands) | `executors/` |
| Launch recovery via receipt file, then `find` (D11 tiers) | `executors/command.py` |
| Failure taxonomy: infra retries under a ceiling, experiment does not | `control/engine.py` |
| Workflow lint: DAG, local ceiling, one metrics stage | `workflow/spec.py` |
| CLI: create, start, seed, run, status, exp-list, history | `cli.py` |

## What is deliberately absent

No proposer, coding agent, analyst, triage, or summarizer. No diff review, protected paths, or
sandboxing beyond the subprocess boundary — which means **this prototype must not run untrusted
code**; that is what doc 08 is for. No replication beyond n=1, no confirmation runs, no
significance testing, no dedup, no budget in dollars, no web inspector.

## Deviations from the spec, and why

**Fencing is enforced in Python, not plpgsql.** Doc 02 puts the check inside a database function
so application code cannot forget it. Here it lives in `store/transitions.py`. The property is
preserved by making that the only writer of state columns and testing it directly
(`test_stale_fencing_token_is_rejected`) rather than by making it structurally impossible.

**`LAUNCH_INTENT → RUNNING` is legal for local stages.** They have no external handle to record,
so routing them through `LAUNCHED` would be ceremony. The state machine caught this the first time
the loop ran, which is the argument for having it.

**`status_map` carries the failure class.** Doc 06 defines one map from raw job status to meaning
(`success` / `running` / `infra` / `experiment` / `triage`). A job system that distinguishes
"preempted" from "failed" already knows the thing the engine most needs and cannot otherwise
infer; `triage` is the honest label for "ambiguous", and falls through to the retry ceiling.

## Three bugs the harness caught

Worth recording, because each is the kind that would be expensive to find in a live campaign.

1. **`LAUNCH_INTENT → RUNNING` rejected.** The state machine refused an illegal move on the first
   run. Without it the row would have silently held an impossible state.
2. **Infinite retry loop.** `_stage_status` reported the *first* failed attempt rather than the
   latest, so `infra_attempt_count` looked permanently zero and the ceiling never fired. Found by
   `test_infra_failure_retries_then_gives_up`. In production this is an unbounded job submitter.
3. **`status_map` doing double duty.** It mapped raw status to both a state and a failure class,
   and the two collided: a `PREEMPTED` job never reached `FAILED` by the intended path, and the
   final attempt was misclassified as an experiment failure — i.e. a preempted node would have
   been reported to the proposer as a research result.

## Next

The seams for step 4 are in place: `agents/` does not exist yet, and the lint rule that it must
never import `store/` is not yet enforced. The proposer slots into `Engine._admit_experiments`,
which currently reads queued hypotheses that a human seeded.
