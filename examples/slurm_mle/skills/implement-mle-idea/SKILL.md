# Implementing an idea in this codebase

Your job is to make the workspace express the idea you were given — nothing
more. A later phase trains and scores it; you are not being asked whether it
is a good idea.

## The workspace

- `config.json` — hyperparameters. If the idea carries a `changes` object,
  apply exactly those keys and leave everything else alone.
- `model.py` — the `MLP` class: `forward`, `train_batch`, and `build`. Change
  it freely for ideas that need it (extra layers, weight decay, a schedule),
  but keep the three entry points intact, because the eval harness calls
  them:
  - `build(config, n_in)` returns a model
  - `model.forward(x)` returns `(prediction, hidden_activations)`
  - `model.train_batch(batch, lr, momentum)` performs one update

## Rules

- **Change only what the idea calls for.** Unrelated "improvements" make the
  result impossible to attribute.
- **Do not touch the eval harness or the data.** They are outside the
  workspace by design; an idea that needs them changed is invalid — report
  `failed` and say so.
- **Keep it runnable.** `python <eval>/train_eval.py --workspace . --out /tmp/x
  --epochs 3` should complete. A crash here wastes the whole trial.
- **Prefer stdlib.** The project has no third-party dependencies and the
  cluster nodes may not have any either.

## Output

Write `result.json` in the phase directory:

- `status: "passed"` — the workspace now expresses the idea
- `status: "failed"` — the idea cannot be implemented as stated (say why in
  `notes`; this is a legitimate outcome, not a failure on your part)

Put what you actually changed in `notes` — that text is what the analysis
phase and the next round of ideation will read.

Do not report accuracy or RMSE numbers. Measurement belongs to the training
phase, and numbers reported here are discarded.
