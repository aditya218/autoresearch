# 08 — Safety Model

D5 puts a coding agent at the head of every experiment: it writes code, and the engine builds and
runs it, unattended, for hours, hundreds of times per campaign. **The agent is untrusted.** Not
because it is malicious, but because it is optimizing a number and will find whatever raises that
number — including things you did not intend to permit.

Three distinct concerns, often conflated:

| Concern | Question | Section |
| --- | --- | --- |
| **Containment** | Can agent code damage anything outside its experiment? | §1–2 |
| **Validity** | Can the agent raise the metric without solving the problem? | §3, doc 07 |
| **Cost** | Can a runaway loop spend unbounded money? | §4 |

---

## 1. Where agent code executes

There are exactly two places, and they have very different risk profiles.

### Local stages — the dangerous one

A stage named `build` running `make build` executes a Makefile the agent may have edited. "Local"
means *on the engine's host*, so a naive implementation runs untrusted code inside the controller
process, with the controller's database credentials and filesystem access. That is the most
serious hazard in the design.

Requirements:

1. **Never in-process.** Local stages run in a subprocess, always. The name `local` is chosen
   over `in_process` specifically to stop the implementation from taking the shortcut.
2. **Sandboxed subprocess**: container or namespace isolation, read-only mounts except the
   experiment's working tree and `artifact_dir`, no network by default (opt in per stage, with an
   allowlist), CPU/memory/wall-clock caps, non-root.
3. **No credentials in the environment.** No `DATABASE_URL`, no cloud credentials, no API keys.
   The git token available to the agent is scoped to push `autoresearch/*` branches on one repo
   and nothing else.
4. **Output is data, never instruction.** Stage stdout is parsed as data. Nothing a stage prints
   is ever interpreted as a command by the controller.

### External jobs — inherited posture

`launch.sh` submits to your existing scheduler, which already has an isolation and quota model
(D15 delegates scheduling entirely). The engine adds only:

- The launch command runs in the same sandbox as local stages — it is a user script, but it takes
  agent-influenced arguments.
- Jobs are tagged with `campaign_id` and `AUTORESEARCH_IDEM_KEY` so orphans are always reapable
  (doc 04 §2).
- Per-experiment resource ceilings are declared in the workflow and enforced by the launcher.

The engine deliberately does **not** try to sandbox your training jobs. That is your scheduler's
job, and pretending otherwise would create a false sense of containment.

---

## 2. Protected paths

The single highest-leverage control, and the main defense against reward hacking. Certain files
define *how success is measured*. An agent that edits them is not doing research.

```yaml
safety:
  protected_paths:
    - "tests/**"
    - "benchmarks/**"
    - "eval/**"
    - "scripts/launch.sh"          # the launcher itself
    - "scripts/metrics.sh"         # metric extraction
    - ".github/**"                 # CI definitions
    - "**/conftest.py"
  protected_path_action: reject     # reject | require_approval
  forbidden_patterns:
    - "eval.*\\.jsonl"              # eval data
    - "**/*.pem"
```

Enforcement is a mandatory `review` stage between `implement` and `build` (doc 06). A diff
touching a protected path is rejected with `experiment_failure` and the reason is fed back to the
proposer — "you may not modify the benchmark" is exactly the kind of negative result that
improves the next proposal.

`require_approval` is available where legitimate cases exist (a benchmark genuinely needs a new
case), and turns the experiment into a blocked `ApprovalRequested` rather than a rejection.

### Diff review checks

The `review` stage runs, in order, aborting on the first rejection:

| Check | Action |
| --- | --- |
| Protected path touched | Per `protected_path_action` |
| Secret introduced (entropy + known-format scan) | Reject, always, and do not echo the secret into the ledger |
| Network egress added where the stage forbids it | Reject |
| Diff size above `max_diff_lines` | Require approval — huge diffs are usually a confused agent |
| Metric-gaming heuristics (hardcoded expected values, benchmark short-circuits, disabled assertions, timing-code edits) | Flag; require approval if `strict_review` |
| Base-commit mismatch | Reject — the branch was not cut from the pinned base |

Heuristic checks produce false positives. They are deliberately set to *flag* rather than reject
by default, because a rejection loop that blocks legitimate work will get switched off entirely,
which is worse than a few reviewed diffs.

---

## 3. Validity controls

Covered in depth in `07-objectives-and-validity.md`; the parts that are safety-enforced here:

- **The engine owns seed selection.** Agent code cannot choose its own seeds.
- **Held-out evaluation is never visible** to the coding agent or the proposer. It runs from a
  protected path, and its data is not present in the agent's working tree.
- **Metric extraction is protected.** `metrics.sh` is a protected path, so the agent cannot
  change how success is computed.
- **Guardrail metrics the objective does not reward** catch changes that win the primary metric
  by sacrificing something unmeasured.

---

## 4. Cost controls

At 1–8 hours per experiment (D7) and hundreds per campaign (D17), the realistic worst case is
not a security breach — it is a crash loop that spends the quarter's compute budget over a
weekend.

| Control | Default |
| --- | --- |
| Campaign budget (USD / GPU-hours / wall-clock / experiment count) | Required in config; no unbounded campaigns |
| Per-experiment cost ceiling | Aborts the experiment when exceeded |
| Budget reservation at admission | Prevents over-committing across 2–4 concurrent experiments |
| Approval gate above a cost threshold | Configurable per campaign |
| Consecutive-infra-failure circuit breaker | 5 → campaign `STOPPING(fatal_error)` |
| Orphan reaping sweep | Every tick; a leaked 8-hour GPU job is the expensive failure mode |
| Kill switch | Immediate: cancel all external jobs, abort in-flight, halt the campaign |

The circuit breaker matters more than it appears. A misconfigured launcher that fails instantly
turns the control loop into a tight retry spin; without a breaker, the engine will happily submit
thousands of doomed jobs overnight.

---

## 5. Credentials

| Principal | Holds | Never holds |
| --- | --- | --- |
| Controller | Postgres credentials, artifact FS write, scheduler submit rights | — |
| Coding agent (local, sandboxed) | Git token scoped to push `autoresearch/*` on one repo; LLM API key | Postgres, cloud, artifact FS root, prod |
| Launch/poll/cancel/find scripts | Whatever your scheduler needs | Postgres, LLM keys |
| External job | Whatever the job needs, per your existing model | Engine credentials |

The agent cannot write to the ledger. All ledger writes go through the controller, which records
what the agent *did* — never what the agent *claims*. An agent that reports its own metrics into
the ledger is an agent that can report whatever metrics it likes.

---

## 6. Attribution and audit

Per D20 (small trusted team, shared instance): attribution everywhere, authorization nowhere.

- Every human-originated event records an actor: `CampaignCreated`, `HypothesisProposed(origin:
  human)`, `ApprovalGranted`, `KillSwitchEngaged`, `CampaignBudgetAdjusted`.
- Every agent-originated hypothesis records `proposer_context_ref` — the exact context that
  produced it.
- Every experiment records the diff that ran.

Together these answer the question that matters after something goes wrong: *what exactly ran,
who or what decided it should, and what did it see when it decided?*

Adding authorization later is additive — a permission check in front of the CLI and API — as long
as attribution is recorded from the start. It is recorded from the start.

---

## 7. What is explicitly out of scope for v1

Stated so the boundaries are deliberate rather than accidental:

- **Multi-tenancy and per-user authorization** (D20). Anyone with CLI access can run anything.
- **Sandboxing external jobs.** Your scheduler's problem.
- **Defending against a deliberately adversarial agent.** The controls above stop an agent that
  cuts corners to raise a metric. They would not stop a capable adversary with code execution and
  a git token, and claiming otherwise would be dishonest. The containment boundary that actually
  holds is the sandbox and the credential scoping, not the diff heuristics.
