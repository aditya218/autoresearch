# 05 — Outer Loop: Proposal, Admission, Stopping

The outer loop is a search policy over the space of hypotheses, driven by the ledger.

```
read ledger ──> propose hypotheses ──> admission control ──> queue
      ▲                                                        │
      └──── analyze results <── experiments execute <── claim ──┘
```

Two decisions shape everything here. The proposer is a **pure LLM** (D6) — there is no classical
sampler to supply diversity, so mode-collapse defenses are mandatory rather than optional. And
experiments run **2–4 at a time** (D3, D8), so the proposer must reason about a small but real
frontier of unresolved work.

---

## Proposer input contract

The proposer never sees the raw ledger. Past a few hundred experiments (D17) it will not fit, and
even when it fits, a wall of JSON produces worse ideas than a curated brief. Input is assembled
deterministically and stored as `proposer_context_ref` on `HypothesisProposed`, so any proposal
can be replayed exactly.

```jsonc
{
  "objective": { /* primary metric, direction, guardrails */ },
  "budget_remaining": { "experiments": 137, "usd": 3200, "hours": 41 },
  "base_commit": "abc123",
  "codebase_context": { /* files the agent may change, architecture notes */ },

  "research_summary": "LLM-maintained digest, every claim citing experiment_ids",
  "leaderboard_top_k": [ /* best feasible experiments: diff summary + metrics + CI */ ],
  "recent_k": [ /* most recent regardless of rank — captures the frontier */ ],
  "negative_results": [ /* experiment_failures with the analyst's explanation */ ],
  "structural_families": [ /* what kinds of change have been tried, and their outcomes */ ],

  "in_flight": [ /* 2-4 hypotheses currently executing: statement + diff summary + ETA.
                    NO results — there are none yet */ ],
  "already_tried": [ /* dedup fingerprints + one-line summaries */ ],
  "rejected_ideas": [ /* with reasons, so it stops re-proposing them */ ],

  "human_notes": [ /* HumanNoteAdded — steering, D14. Read on every proposal */ ],
  "seed_context": { /* priors, prior art, constraints; inherited from parent if forked */ }
}
```

`in_flight` carries no results because there are none — that is the point. The proposer is told
what is running and instructed either to propose something informative regardless of how those
resolve, or to explicitly build on a pending result and accept the risk.

`structural_families` exists specifically to fight mode collapse: it makes "we have tried seven
variants of kernel fusion and two of memory layout" visible as a fact rather than something the
proposer must infer from a list of diffs.

### The research summary (D12)

An LLM-maintained digest, updated incrementally after each experiment completes rather than
regenerated. It is what keeps the campaign coherent past the context limit.

Structure: objective → what has been established (with experiment_ids) → what has been ruled out
→ open questions → current best and why → frontier of uncertainty.

**Every claim must cite experiment_ids.** This is enforced by a validator, not a prompt request:
a summary sentence asserting a result with no citation is rejected and regenerated.

**Drift audit.** Each pass compresses lossily, and after fifty passes the summary can assert
things the ledger does not support. Mitigations: (a) citations are checked to resolve to real
experiments with the claimed outcome; (b) every N experiments (default 25) the summary is
re-derived from the log independently and diffed against the incremental version, with
divergence logged; (c) summary versions are retained at `summary/{version}.md`, so a bad pass can
be rolled back rather than being permanently baked in.

---

## Proposer output contract

Because the coding agent implements the change (D5), a hypothesis is a **change specification**,
not a parameter vector.

```jsonc
{
  "hypotheses": [{
    "statement": "Fusing the KV-cache write into the attention epilogue will cut p50 latency",
    "rationale": "exp_07 showed the epilogue is 18% of step time; exp_11 ruled out ...",
    "derived_from": { "experiment_ids": ["exp_07", "exp_11"] },
    "change_spec": {
      "intent": "Fuse the cache write into the epilogue, avoiding the extra pass",
      "files_of_interest": ["src/attention/epilogue.cu"],
      "constraints": ["must not change numerics beyond 1e-5", "keep the fallback path"],
      "acceptance": "compiles, unit tests pass, benchmark reports p50"
    },
    "parameters": { "block_size": 256 },
    "structural_family": "kernel-fusion",
    "novelty_justification": "differs from exp_11 in fusing the write rather than the read",
    "predicted_effect": { "metric": "p50_latency_ms", "direction": "down",
                          "magnitude": 0.12, "confidence": 0.4 },
    "predicted_cost": { "usd": 40, "hours": 2 },
    "kill_criteria": "abort if step time regresses >5% at 200 steps"
  }],
  "reasoning_trace": "..."
}
```

Three fields earn their place:

- **`predicted_effect`** is scored against reality on completion, making proposer calibration a
  measurable quantity. A proposer whose predictions are uncorrelated with outcomes is doing
  random search with extra steps, and you want to know that by experiment 30, not experiment 300.
- **`structural_family`** is the mode-collapse instrument. Self-declared, then validated against
  the diff after implementation.
- **`kill_criteria`** is recorded but **not enforced in v1** (D24) — without a cancel command
  there is nothing to act on it. It is still worth collecting: the analyst uses it to judge
  whether an experiment went the way the proposer expected, and it becomes live the moment a
  cancel tool exists.

---

## Mode collapse

The characteristic failure of a pure-LLM proposer (D6): it finds a promising direction and
proposes an unbounded sequence of minor variations on it, converging on a local optimum while
reporting steady tiny improvements. With no sampler injecting diversity, the engine must supply it.

| Defense | Mechanism |
| --- | --- |
| Exploration quota | At least `min_exploration_fraction` (default 0.3) of proposals must open a *new* structural family, enforced at admission |
| Family saturation | After K consecutive experiments in one family with no improvement (default 5), that family is marked saturated and shown as such in the brief |
| Novelty requirement | Every hypothesis states how it differs from its nearest tried neighbour; admission rejects vacuous differentiators |
| Diversity escalation | When the top-k leaderboard stops changing, the brief explicitly instructs exploration and the exploration quota rises |
| Diversity collapse stop | M consecutive rounds of duplicate-only proposals (default 5) stops the campaign — it has nothing left to say |

The exploration quota is the important one, and it will feel wasteful in the middle of a campaign
when the exploit direction is producing gains. It is worth it: the alternative is a campaign that
spends 200 experiments polishing the first idea it had.

---

## Admission control

Every proposal passes this gate before entering the queue. It is deliberately not the proposer's
job — a component that both generates and approves its own work has no check on it.

| Check | Action on failure |
| --- | --- |
| Schema validity of `change_spec` and `parameters` | `REJECTED(policy)`; one repair round-trip allowed |
| Exact dedup on `dedup_fingerprint` | `REJECTED(duplicate, duplicate_of)` |
| Near-dedup on statement embedding | `REJECTED(duplicate)` unless a non-vacuous differentiator is declared |
| Exploration quota not met | Deferred, with the brief re-issued demanding a new family |
| Safety policy — targets protected paths, exceeds blast radius (doc 08) | `REJECTED(unsafe)`, always logged |
| Budget feasibility against `predicted_cost` | `REJECTED(out_of_budget)` or deferred |
| Approval gate — cost above threshold | `ApprovalRequested`; queued blocked |

**Post-implementation dedup.** The strongest dedup happens *after* the coding agent runs: if the
resulting diff is byte-identical to a previous experiment's, short-circuit with
`ExperimentResultReused` and skip the 1–8 hour job entirely. Pre-execution semantic dedup is
approximate; diff-identity is exact.

### Staleness

With 2–4 concurrent experiments (D8), the queue is kept shallow — propose only when queue depth
falls below the concurrency limit — so ideas rarely age. The staleness machinery is therefore
minimal:

- Record `proposed_at_experiment_count`.
- On claim, if more than `staleness_threshold` (default 10) experiments have completed since,
  re-validate with a cheap proposer call returning `still_valid` / `revise` / `obsolete`.
- `obsolete` → `HypothesisExpired`. Revised → a new hypothesis with `derived_from` lineage.

---

## Human steering (D14)

Two intervention channels, both recorded as events, both visible in the audit trail:

1. **Injected hypotheses** — `origin: human`, entering the queue at a configurable priority.
   They bypass the exploration quota (a human asking for something specific is not mode collapse)
   but not safety checks.
2. **Steering notes** — `HumanNoteAdded`, included in every subsequent proposer brief. "Stop
   trying memory-layout changes, we know that path is blocked by the allocator" is the cheapest
   possible intervention and frequently rescues a campaign that has gone down a dead end.

Notes are additive and timestamped. A note is never silently dropped; if the brief must be
truncated, notes are the last thing cut.

---

## Stopping criteria

Evaluated every tick. First to fire wins; all recorded in `CampaignStopped`.

| Criterion | Default |
| --- | --- |
| Budget exhausted (experiments / USD / GPU-hours / wall-clock) | Config-required — no unbounded campaigns |
| Target reached — primary metric hits target with guardrails satisfied | Optional |
| Converged — no leaderboard improvement in N completed experiments | N = 40 |
| Diversity collapse | M = 5 duplicate-only rounds |
| Circuit breaker — consecutive infra failures | 5 |
| Manual stop / kill switch | Halts admission; in-flight work completes (D24) |

**Confirmation before completion.** On stop, the top candidate is re-run at
`confirmation_replicates` (default 5) with fresh, disjoint seeds and evaluated on the held-out
set before being reported as the winner. After a few hundred experiments the leaderboard argmax
is substantially selection noise; this is the cheapest defense against shipping a mirage. See
`07-objectives-and-validity.md`.
