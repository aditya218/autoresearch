# autoresearch

An engine for running research campaigns carried out autonomously by agents.

- **Inner loop** — the user-configured, multi-stage workflow for a single experiment, executed
  as a durable state machine.
- **Outer loop** — an autonomous search: read the ledger of prior experiments, propose a new
  hypothesis, evaluate it, repeat.
- **Research ledger** — an append-only event log in Postgres. Campaigns are durable and
  resumable after a crash with minimal to no wasted work.

Specifications live in [`docs/spec/`](docs/spec/README.md). Implementation has not started;
[`docs/spec/OPEN-QUESTIONS.md`](docs/spec/OPEN-QUESTIONS.md) lists what is still undecided.
