# Autoresearch Engine — Specification

An engine for running **research campaigns** that are carried out autonomously by agents.

Two loops:

- **Inner loop** — the workflow for a *single experiment*. User-configured, multi-stage,
  executed as a durable state machine.
- **Outer loop** — an autonomous search for a better solution: read the ledger of prior
  experiments, propose a new hypothesis, materialize it into an experiment, evaluate, repeat.

All state lives in a durable, append-only **research ledger**. A campaign can be killed at
any instant and resumed with minimal to no wasted work.

## Document index

| Doc | Contents | Status |
| --- | --- | --- |
| [01-data-model.md](01-data-model.md) | Entities, relationships, SQL schema | Draft — stable |
| [02-event-log.md](02-event-log.md) | Ledger as event log, event catalog, projections | Draft — stable |
| [03-lifecycle.md](03-lifecycle.md) | State machines for every entity | Draft — stable |
| [04-durability.md](04-durability.md) | Leases, fencing, idempotency, re-attach, recovery | Draft — stable |
| [05-outer-loop.md](05-outer-loop.md) | Proposer contract, admission control, stopping rules | Draft — depends on open Qs |
| [06-inner-loop.md](06-inner-loop.md) | Workflow spec, stage contract, stage kinds | Draft — depends on open Qs |
| [07-objectives-and-validity.md](07-objectives-and-validity.md) | Metric contract, noise, reward-hacking defenses | Draft — depends on open Qs |
| [OPEN-QUESTIONS.md](OPEN-QUESTIONS.md) | Unresolved decisions blocking the above | Open |

## Decisions locked

| # | Decision | Choice |
| --- | --- | --- |
| D1 | `run` semantics | Execution session holding a **lease** on the campaign. Experiments are not owned by runs. |
| D2 | Durability substrate | **Event-sourced append-only log in Postgres.** Ideas, experiments, leaderboards are projections. |
| D3 | Concurrency | **Parallel** experiments per campaign; the proposer sees in-flight, unresolved experiments. |
| D4 | Stage execution | **Both** in-process stages and external-job stages with re-attach, declared per stage kind. |

## Design principles

1. **The log is the truth.** Every mutation is an event. Projections are disposable and
   rebuildable. If a projection and the log disagree, the log wins.
2. **Nothing external happens without a recorded intent.** Intent is written and committed
   *before* the side effect, carrying an idempotency key that is also stamped onto the
   external resource. Recovery reconciles by that key.
3. **One writer per campaign.** Enforced by lease + fencing token, checked on every append.
4. **Config is immutable once running.** Editing a running campaign forks a new campaign.
   Comparability of results depends on it.
5. **Infra failure is not a research result.** The failure taxonomy is load-bearing: what the
   proposer learns and what the budget is charged both depend on it.
