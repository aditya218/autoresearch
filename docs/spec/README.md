# Autoresearch Engine — Specification

An engine for running **research campaigns** that are carried out autonomously by agents.

Two loops:

- **Inner loop** — the workflow for a *single experiment*. User-configured, multi-stage,
  executed as a durable state machine.
- **Outer loop** — an autonomous search for a better solution: read the ledger of prior
  experiments, propose a new hypothesis, materialize it into an experiment, evaluate, repeat.

All of it is recorded in a durable **research ledger** — state tables in Postgres holding current
truth, alongside an append-only transition log for debugging. A campaign can be killed at any
instant and resumed with minimal to no wasted work.

## The shape of one experiment

```
hypothesis ──> coding agent writes a change on a git branch off a pinned base commit
                │
                ├──> local stages: build, compile check, diff review        (cheap, re-runnable)
                │
                └──> external job: user's launch.sh submits from that commit, returns job_id
                          engine polls by job_id for hours, re-attaches after any crash
                          │
                          └──> metrics ──> aggregate ──> analyze ──> back into the ledger
```

## Document index

| Doc | Contents |
| --- | --- |
| [01-data-model.md](01-data-model.md) | Entities, relationships, SQL schema |
| [02-state-and-history.md](02-state-and-history.md) | State transitions, CAS + fencing, the debug log |
| [03-lifecycle.md](03-lifecycle.md) | State machines for every entity |
| [04-durability.md](04-durability.md) | Leases, fencing, idempotency, re-attach, recovery |
| [05-outer-loop.md](05-outer-loop.md) | Proposer contract, admission control, stopping rules |
| [06-inner-loop.md](06-inner-loop.md) | Workflow spec, command executor, stage contract |
| [07-objectives-and-validity.md](07-objectives-and-validity.md) | Metric contract, noise, reward-hacking defenses |
| [08-safety.md](08-safety.md) | Sandboxing, protected paths, diff review, fidelity, credentials |
| [09-interfaces.md](09-interfaces.md) | CLI, web inspector, package layout |
| [DECISIONS.md](DECISIONS.md) | Every resolved decision with rationale, and what remains open |

## Decisions

| # | Decision | Choice |
| --- | --- | --- |
| D1 | `run` semantics | Execution session holding a **lease** on the campaign. Experiments are not owned by runs. |
| D2 | Durability substrate | **Mutable state tables in Postgres are the source of truth**, plus an append-only transition log for debugging only. |
| D3 | Concurrency model | **Parallel** experiments; the proposer sees in-flight, unresolved experiments. |
| D4 | Stage execution | **Both** local stages and external jobs with re-attach, declared per stage. |
| D5 | What an experiment is | **An agent writes and runs code.** Branch per experiment off a pinned base commit. |
| D6 | Proposer strategy | **Pure LLM.** No classical sampler in v1; mode-collapse defenses are mandatory. |
| D7 | Experiment duration | **1–8 hours.** Re-attach is essential; checkpointing optional. |
| D8 | Concurrency target | **2–4** experiments in flight per campaign. |
| D9 | Domain | **Domain-agnostic core.** The engine never learns what a job is. |
| D10 | Stage authoring | **User-supplied commands**: `launch`, `poll`, `find`, plus `logs` for triage. |
| D11 | Launch recovery | **Tiered**: receipt file (default) → `find` by tag (optional) → relaunch and flag. |
| D12 | Research summary | **LLM-maintained**, every claim citing experiment_ids, with a periodic drift audit. |
| D13 | Cross-campaign visibility | **Strict isolation**, except a forked campaign inherits its parent's ledger read-only. |
| D14 | Human intervention | Humans may **inject hypotheses and steer the proposer** mid-campaign. |
| D15 | Scheduling | **Delegated entirely** to the user's existing scheduler. |
| D16 | Storage | **Postgres** for the log and queryable state; **distributed FS** at `{project_id}/{campaign_id}/…` for artifacts. |
| D17 | Scale target | Hundreds of experiments per campaign, tens of campaigns per project. No partitioning in v1. |
| D18 | Config mutability | Immutable for **measurement and execution**; editable in place for **search policy and budget**. |
| D19 | Deliverable | **Report + winning branch.** The engine does not open PRs. |
| D20 | Users | **Small trusted team, shared instance.** Attribution yes, authorization no. |
| D21 | Interface | **CLI + read-only web inspector.** |
| D22 | Stack | **Python, minimal dependencies.** No workflow framework; the engine owns its control loop. |
| D23 | Objectives | **Constrained scalar.** One primary metric, others as guardrails. No Pareto frontier. |
| D24 | Cancelling jobs | **Deferred.** Jobs run to completion; every stop path gates admission, not execution. |
| D25 | Failure classification | **Status map first, agent triage on ambiguity**, with an absolute retry ceiling behind both. |
| D26 | Coding agent iteration | **Iterates on mechanical failures** (compile, lint, import) inside the stage; semantic failures surface untouched. |

## Design principles

1. **The state table is the truth.** Transitions are compare-and-swap with a fencing check. The
   transition log is written in the same transaction, and **nothing in the control path reads
   it** — that rule is what keeps it a debugging aid rather than a second source of truth.
2. **Nothing external happens without a recorded intent.** The intent row is committed *before*
   the side effect, carrying a deterministic key. How much of the remaining ambiguity gets
   resolved is tiered (D11), not absolute — and the tier in use is recorded rather than assumed.
3. **One writer per campaign.** Enforced by lease + fencing token, checked on every state change.
4. **The filesystem is never authoritative for state.** Recovery reads Postgres to learn what
   state things are in. The one carve-out is the launcher's receipt file, which is evidence about
   an external effect rather than state — the same role `find` plays.
5. **The engine does not know what a job is.** It submits a command, polls a job_id, and reads
   a metrics file. Everything domain-specific lives in the user's scripts.
6. **Infra failure is not a research result.** What the proposer learns and what the budget is
   charged both depend on the failure taxonomy.
7. **The agent is untrusted.** It writes code that the engine executes. Every safety property in
   doc 08 follows from taking that seriously.
