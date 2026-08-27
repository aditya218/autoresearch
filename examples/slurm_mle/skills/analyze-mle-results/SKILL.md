# Analyzing a training run

Write down what this trial actually showed, for whoever reads it next —
which is usually the ideator choosing the following experiment. Your report
is the campaign's memory of this trial.

## What to read

- `../train/metrics.json` — `val_rmse`, `train_rmse`, `seconds`, the `config`
  that produced them, and `history` (validation RMSE every ten epochs)
- `../smoke_test/metrics.json` — the five-epoch gate result
- The campaign index, for how this trial compares to the baseline and to its
  parent

## What a useful report says

1. **What changed, and what happened to the metric.** State the delta against
   the baseline and against the parent trial, not just the absolute number.
2. **Whether the change explains the result.** A lower RMSE right after adding
   momentum is evidence; a lower RMSE with three simultaneous changes is not.
3. **What the training curve shows.** `history` distinguishes *converged*
   from *still improving when epochs ran out* — the latter means the next
   idea should be "train longer", not "different architecture".
4. **Underfitting or overfitting.** Compare `train_rmse` and `val_rmse`: close
   together and both high means underfitting; a large gap means overfitting.
5. **What to try next, and why.** One or two concrete suggestions grounded in
   the above.

## Be honest about small differences

This task is noisy at the third decimal. A change of ±0.005 in `val_rmse` is
not evidence of anything by itself — say so rather than declaring a winner.
If a difference matters, it should be visible against the baseline gap, not
buried in noise. Never round a number in a favourable direction, and never
report a number you did not read from a metrics file.

If the run diverged, say what likely caused it (learning rate, weight scale)
so nobody repeats it.

## Output

Write `report.md` in the phase directory with your analysis, then
`result.json`:

```json
{"status": "passed", "notes": "one-sentence summary of the finding",
 "artifacts": ["report.md"]}
```

Use `status: "passed"` whenever the analysis is complete — it describes
*your* work, not whether the experiment succeeded. A trial whose idea made
things worse still gets a `passed` analysis, because the analysis was done.

Do not report `val_rmse` in `metrics`; it belongs to the training phase and
would be discarded here.
