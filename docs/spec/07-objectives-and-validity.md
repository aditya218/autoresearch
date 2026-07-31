# 07 — Objectives, Noise, and Validity

> **Status:** the mechanism is settled; thresholds and the held-out policy depend on
> `OPEN-QUESTIONS.md` Q1 (domains) and Q8 (experiment duration).

This document covers the largest gap in the original design — there was no definition of what a
*result* is — and the largest scientific risk in any autonomous research loop: the engine
optimizing something other than what you meant.

---

## Metric registry (project level)

Defined at the project so results are comparable across campaigns.

```jsonc
{
  "p50_latency_ms": { "type": "float", "unit": "ms",  "direction": "minimize",
                      "noise": "moderate", "aggregation": "mean" },
  "eval_quality":   { "type": "float", "unit": "score", "direction": "maximize",
                      "noise": "high", "aggregation": "mean" },
  "peak_memory_gb": { "type": "float", "unit": "GB",  "direction": "minimize",
                      "noise": "low",  "aggregation": "max" }
}
```

A campaign may **select** and **constrain** metrics but may not redefine a metric's meaning, unit,
or direction. Doing so would silently invalidate cross-campaign comparison, so it is rejected.

## Objective spec (campaign level)

```jsonc
{
  "primary": { "metric": "p50_latency_ms", "direction": "minimize" },
  "constraints": [
    { "metric": "eval_quality",   "op": ">=", "value": 0.93, "kind": "guardrail" },
    { "metric": "peak_memory_gb", "op": "<=", "value": 40,   "kind": "guardrail" }
  ],
  "comparison": {
    "rule": "constrained_argmin",
    "min_improvement": { "absolute": 0.5, "relative": 0.01 },
    "significance": { "method": "welch_t", "alpha": 0.05, "required_for": "leaderboard_top" }
  },
  "aggregation": { "over_replicates": "mean", "dispersion": "stddev",
                   "interval": "t_95", "high_variance_threshold": 0.15 }
}
```

- **Guardrail violated ⇒ infeasible, not failed.** The experiment `SUCCEEDED`; it is excluded
  from the leaderboard by the feasibility filter and shown to the proposer as an informative
  result. Marking it failed would hide a real finding.
- **`min_improvement`** stops the leaderboard churning on noise-sized deltas and stops the
  proposer chasing them.
- **Multi-objective** (a true Pareto frontier rather than constrained scalar) is deferred.
  Constrained-scalar covers the stated use cases; if a project genuinely needs a frontier, the
  leaderboard projection becomes a non-dominated set and the proposer is asked to expand the
  frontier rather than to beat a scalar. Flagged in open questions.

---

## Noise

If the objective is noisy and the engine ignores it, the campaign optimizes the seed.

1. **Declare noise per metric.** `low` → 1 replicate. `moderate`/`high` → `default_replicates`
   from campaign config, minimum 3.
2. **Sequential replication, not fixed.** Run 1 replicate; only add replicates if the result is
   competitive with the leaderboard top. Spending 5 seeds on an obviously-bad idea is waste.
3. **Report intervals, not point values.** Everything the proposer sees carries dispersion and n.
   A point estimate presented alone invites the proposer to over-read a 0.3% delta.
4. **Flag `high_variance`** when replicate dispersion exceeds threshold — a signal to the
   proposer and a trigger for the confirmation policy.
5. **Fixed seed sets per role.** Exploration seeds and *confirmation* seeds are disjoint. If the
   confirmation run reuses exploration seeds it re-confirms the same luck.

---

## Reward hacking and validity

The top failure mode of autonomous research loops. The engine is optimizing a number; anything
that raises the number without solving the problem is, from its perspective, a success.

| Attack | Defense |
| --- | --- |
| Overfitting the eval set across many experiments | **Held-out set the proposer never sees.** Report dev metrics during search; validate the winner on held-out before declaring it. Divergence between dev and held-out is itself a logged finding |
| Train/test contamination introduced by generated code | Contamination checks as a mandatory workflow stage; dataset fingerprinting; provenance pinning of dataset_version |
| Gaming the metric (special-casing the benchmark, caching the answer, disabling the check) | Guardrail metrics the objective does not reward; a `verify` stage that re-runs the metric independently of the code under test; diff review on generated code for suspicious patterns |
| Weakening the test rather than fixing the code | Treat test/benchmark files as protected paths; any diff touching them is flagged for approval |
| Cherry-picking seeds | Engine owns seed selection; the workflow cannot choose its own seeds |
| Winner's curse across many experiments | **Confirmation runs** (below) |
| Silent environment drift making old results incomparable | Provenance drift detection (`06-inner-loop.md`) |

### Confirmation policy

After ~200 experiments against a noisy objective, the leaderboard argmax is substantially
selection noise. Before a campaign reports a winner:

1. Re-run the top candidate at `confirmation_replicates` with fresh, disjoint seeds
   (`role = confirmation`).
2. Evaluate on the held-out set.
3. Compare against the *incumbent baseline*, not against the campaign's best dev score.
4. If the confirmed effect falls below `min_improvement`, report **no winner** and say so
   plainly, with the shrinkage from dev to confirmation.

A campaign that honestly reports "no reliable improvement found" is functioning correctly. An
engine that cannot produce that outcome is not measuring anything.

### Baselines

Every campaign runs a `role = baseline` experiment first — the unmodified configuration through
the identical workflow. Without it there is no reference point, and no way to detect that the
whole harness has drifted. Re-run the baseline periodically (default every 50 experiments) so
harness drift shows up as baseline movement rather than as a phantom improvement.

---

## What a campaign reports

```
Campaign <id> — <objective> — stopped: converged (no improvement in 40)
Baseline:  p50 142.0ms  (n=5, ±2.1)
Winner:    exp_0173     p50 118.4ms (n=5, ±3.0)  −16.6%  [confirmed, held-out]
           guardrails:  eval_quality 0.937 ✓   peak_memory 31.2GB ✓
           change:      fused KV-cache epilogue, block_size=256
           lineage:     hyp_0161 <- exp_0084, exp_0119
Runner-up: exp_0155     p50 121.0ms (n=5, ±2.8)  not significantly different from winner
Ruled out: 14 structural families (see report)
Cost:      $3,180 / 61h wall / 412 GPU-hours across 173 experiments
Caveats:   dev→held-out shrinkage 3.1pp; 2 experiments invalidated (provenance drift)
```

The caveats line is not decoration. A campaign summary without shrinkage, invalidations, and
variance is the kind of report that gets a bad result shipped.
