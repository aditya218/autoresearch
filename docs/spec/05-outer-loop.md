# 05 — Outer Loop: Proposal, Admission, Stopping

> **Status:** structure is settled; several parameters depend on answers in `OPEN-QUESTIONS.md`
> (Q4 proposer strategy, Q5 context policy, Q6 cross-campaign visibility, Q7 human injection).

The outer loop is a search policy over the space of hypotheses, driven by the ledger.

```
read ledger ──> propose N hypotheses ──> admission control ──> queue
      ▲                                                          │
      └──── analyze results <── experiments execute <── claim ────┘
```

Because concurrency is **parallel with in-flight visibility** (D3), the proposer must reason
about experiments that are running but unresolved. This is the async-batch problem from Bayesian
optimization, and ignoring it produces the characteristic failure: five simultaneous variants of
the same idea, four of them redundant.

---

## Proposer input contract

The proposer never sees the raw ledger — it will not fit past ~50 experiments, and even when it
does fit, a wall of JSON produces worse ideas than a curated brief. Input is assembled
deterministically and stored content-addressed as `proposer_context_ref` on
`HypothesisProposed`, so any proposal can be replayed exactly.

```jsonc
{
  "objective": { /* primary metric, direction, constraints, guardrails */ },
  "budget_remaining": { "experiments": 137, "usd": 3200, "hours": 41 },
  "search_space": { /* declared parameter space + free-form allowance */ },

  "research_summary": "incrementally maintained digest — what has been learned so far",
  "leaderboard_top_k": [ /* best feasible experiments, full config + metrics */ ],
  "recent_k": [ /* most recent experiments regardless of rank — captures the frontier */ ],
  "negative_results": [ /* experiment_failures with analysis: what breaks and why */ ],

  "in_flight": [ /* hypothesis statement + parameters + ETA, NO results — D3 */ ],
  "already_tried": [ /* dedup fingerprints + one-line summaries, cheap to include */ ],
  "rejected_ideas": [ /* with rejection reasons, so it stops re-proposing them */ ],

  "human_notes": [ /* HumanNoteAdded events — steering the campaign mid-flight */ ],
  "seed_context": { /* campaign.seed_context: priors, prior art, constraints */ }
}
```

`in_flight` carries **no results** because there are none — that is the point. The proposer is
told what is being tried right now and instructed to propose ideas that remain informative
regardless of how those resolve, or to explicitly build on a pending result and accept the risk.

### The research summary

An incrementally-maintained digest, updated after each experiment completes rather than
regenerated from scratch. It is the mechanism that keeps the campaign coherent past the context
limit. Maintained as its own projection, versioned so a bad summarization pass can be rolled back.

Suggested structure: what the objective is → what has been established (with experiment_ids) →
what has been ruled out → open questions → current best and why → the frontier of uncertainty.

**Risk to design against:** summary drift. Facts get compressed lossily each pass and eventually
the summary asserts things the ledger does not support. Mitigation: every claim in the summary
must cite experiment_ids, and a periodic audit re-derives the summary from the log and diffs it.

---

## Proposer output contract

```jsonc
{
  "hypotheses": [{
    "statement": "Fusing the KV-cache write with the attention epilogue will cut p50 latency",
    "rationale": "exp_07 showed the epilogue is 18% of step time; exp_11 ruled out ...",
    "derived_from": { "experiment_ids": ["exp_07", "exp_11"] },
    "parameters": { /* typed, validated against the workflow's input schema */ },
    "predicted_effect": { "metric": "p50_latency_ms", "direction": "down",
                          "magnitude": 0.12, "confidence": 0.4 },
    "predicted_cost": { "usd": 40, "hours": 2 },
    "novelty_justification": "differs from exp_11 in that ...",
    "kill_criteria": "abort if step time regresses >5% at 200 steps"
  }],
  "reasoning_trace": "..."
}
```

Two fields earn their place:

- **`predicted_effect`** — scored against reality on completion. The proposer's calibration
  becomes a measurable, trackable quantity, which is both a quality signal for the campaign and
  a strong input to exploration/exploitation balance. A proposer whose predictions are
  uncorrelated with outcomes is doing random search with extra steps, and you want to know that.
- **`kill_criteria`** — enables early abort of doomed experiments, which is where a lot of budget
  is otherwise burned. Requires the workflow to expose intermediate metrics (doc 06).

---

## Admission control

Every proposal passes this gate before entering the queue. This is the safety and efficiency
choke point, and it is deliberately not the proposer's job — a component that both generates and
approves its own work has no check on it.

| Check | Action on failure |
| --- | --- |
| Schema validity — `parameters` typecheck against workflow input schema | `REJECTED(policy)`; optionally one repair round-trip |
| Exact dedup — `dedup_fingerprint` collides with an existing hypothesis | `REJECTED(duplicate, duplicate_of)` |
| Near-dedup — embedding similarity above threshold to a tried idea | `REJECTED(duplicate)` unless it declares an explicit differentiator |
| Result cache — `resolved_config_hash` matches a succeeded experiment with identical provenance | Admit, then short-circuit via `ExperimentResultReused` |
| Safety policy — touches prohibited resources, exceeds blast radius | `REJECTED(unsafe)`, always logged |
| Budget feasibility — `predicted_cost` exceeds remaining budget | `REJECTED(out_of_budget)` or deferred |
| Approval gate — cost above threshold, or a flagged action class | `ApprovalRequested`; queued as blocked pending human decision |

### Staleness

An idea proposed at ledger depth 12 may be answered or invalidated by depth 40. Policy:

- Record `proposed_at_experiment_count` at proposal.
- On claim, if `current_count - proposed_at_count > staleness_threshold` (default 10) **or**
  any experiment completed since that shares a dedup neighbourhood with this idea, re-validate:
  a cheap proposer call that returns `still_valid` / `revise(parameters)` / `obsolete`.
- `obsolete` → `HypothesisExpired`. Revised → new hypothesis with `derived_from` lineage.

Cheaper alternative if re-validation proves expensive in practice: keep the queue shallow (only
propose when below the low watermark) so ideas rarely age. Both are compatible; start with the
shallow queue and add re-validation if staleness shows up in the audit.

---

## Exploration policy

**OPEN (Q4).** The structural choice:

- **Pure LLM proposer** — flexible, handles structural/code-level changes, weak at numeric
  optimization, prone to mode collapse (proposing minor variants of the current best forever).
- **Hybrid** — LLM proposes the *structure* of a change; a classical optimizer (TPE, GP-BO,
  bandit) selects numeric parameters within the structure it opens up. Strictly stronger where
  the space is partly numeric, and it directly counteracts mode collapse.

Recommendation: design the interface for hybrid from the start — the proposer emits a
*parameterized family* plus a search space, and a sampler chooses points within it. A pure-LLM
proposer is then the degenerate case that emits a family with zero free parameters. Retrofitting
this later means rewriting the hypothesis schema.

Mode-collapse defenses regardless of choice: enforce a minimum exploration fraction (some
proportion of proposals must differ structurally, not parametrically, from the current best);
track distinct structural families tried; escalate diversity pressure when the leaderboard
top-k converges.

---

## Stopping criteria

Evaluated every tick. First to fire wins; all are recorded in `CampaignStopped`.

| Criterion | Default |
| --- | --- |
| Budget exhausted (experiments / USD / GPU-hours / wall-clock) | Config-required — no unbounded campaigns |
| Target reached — primary metric hit `target_metric_value` with guardrails satisfied | Optional |
| Converged — no leaderboard improvement in N completed experiments | N = 40 |
| Diversity collapse — proposer emits only duplicates for M consecutive rounds | M = 5 |
| Manual stop / kill switch | — |
| Fatal error — repeated infra failure above threshold, config drift detected | — |

**Confirmation before completion.** On stop, the top candidate is re-run at
`confirmation_replicates` (default 5) with a fresh seed set before being reported as the winner.
After 200 experiments, the argmax of a noisy objective is very likely partly noise; this is the
single cheapest defense against shipping a mirage. See `07-objectives-and-validity.md`.
