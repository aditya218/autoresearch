# Autoresearch v2

An autonomous research engine for ML experimentation. Agents generate ideas
grounded in the research so far, push each idea through a configurable
experiment workflow, and everything is recorded durably.

The architecture in one line: **a small deterministic engine that calls agents
at defined points — never an agent that runs the show.** The engine's behavior
is a pure function of its event log and config, which is what makes budgets,
crash recovery, and audit trustworthy.

See [design.md](design.md) for the full design.

## Quick start

```bash
uv sync

# check a campaign config: phase DAG, gates, metric bindings, scripts
uv run autoresearch validate toy_project/campaign.yaml

# run one phase in isolation - how projects develop their scripts and skills
uv run autoresearch run-phase toy_project/campaign.yaml train \
    --workspace /tmp/ws --out /tmp/phase

# run one idea through the whole workflow, no campaign
uv run autoresearch run-one toy_project/campaign.yaml --campaign-dir /tmp/c

# run a campaign; --harness names any agent CLI, --ideate lets it propose ideas
uv run autoresearch run toy_project/campaign.yaml --campaign-dir /tmp/c \
    --harness 'claude -p' --ideate --vcs git --repo . --mirror /path/to/remote

uv run autoresearch status /tmp/c
```

## What a project supplies

The engine contains no project-specific logic. A project brings:

- **`campaign.yaml`** — goal, key metrics, ideation settings, and the workflow
  (a DAG of phases, each agentic or deterministic, some of them gates).
- **Scripts** — `launch` (prints a job id), `poll`, `collect`, and optionally
  `find` (job ids by tag) for remote jobs; `run` for local phases.
- **Skills** — what agentic phases, ideation, and repair actually know how to
  do.

`toy_project/` is a complete working example whose "remote" jobs are fake, so
the whole engine can be exercised in seconds.

## Layout

| Module | What it does |
|---|---|
| `events.py`, `ledger.py` | Append-only JSONL event log: the source of truth, with crash recovery |
| `state.py`, `views.py` | Derived state by replay, and the readable views agents and humans read |
| `config.py`, `contract.py` | Campaign schema, and the `result.json` phase contract |
| `project.py`, `phases.py` | Project scripts, and running one phase |
| `engine.py` | `run_trial`: the workflow walk, gates, retries, provenance, resume |
| `loop.py` | The campaign loop: baseline, ideation, admission, budgets |
| `agents.py`, `agentic.py` | The pluggable agent-harness adapter |
| `ideator.py`, `tools.py` | Agent-backed ideation; engine-mediated `launch_job` |
| `repair.py` | Best-effort recovery when a job hits a situation with no rule |
| `sync.py`, `vcs.py` | Remote-filesystem mirroring; trial workspaces |

## Tests

```bash
uv run pytest
```

Includes crash/recovery tests that SIGKILL a writer mid-stream and assert the
ledger recovers, and full campaigns run against the toy project.
