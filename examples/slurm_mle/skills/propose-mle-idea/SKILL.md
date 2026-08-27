# Proposing ideas for this task

You are choosing the next experiments for a small regression model. Every
idea you propose costs a cluster allocation, so propose ideas that would
teach you something whether they win or lose.

## What you can change

Only two files exist in the workspace, and both are fair game:

- `config.json` — `hidden_size`, `activation` (`tanh` / `relu` / `sigmoid`),
  `learning_rate`, `momentum`, `weight_decay`, `epochs`, `batch_size`,
  `weight_scale`, `seed`
- `model.py` — the architecture itself: layer count and widths, the
  initialisation, the update rule, learning-rate schedules, and the
  `prepare(train_rows)` / `transform(x)` hooks, which are where anything
  fitted on the training data belongs

**Read the data before proposing.** `eval/data.py` describes the task, and
what it says about the inputs matters more than any hyperparameter. Ideas
that only edit `config.json` tend to plateau on this task; the ones that
pay attention to what the model is being fed do not.

The dataset and the scoring are fixed and out of bounds. An idea that
proposes changing them is invalid.

## What makes a good idea here

- **Test one hypothesis at a time.** "Momentum 0.9" teaches you something;
  "momentum 0.9 plus relu plus twice the width" teaches you nothing about
  which part helped.
- **Read the results so far before proposing.** If a wider network already
  failed to help, propose something else — not a slightly wider one.
- **Prefer ideas that a result can settle.** "Add momentum" is settleable;
  "make it better" is not.
- **Branch deliberately.** When an idea builds on a specific earlier trial's
  code, set `parent_trial` to that trial id so the work starts from its
  state rather than the baseline.
- **Capacity and optimisation are different problems.** If train and val RMSE
  are both high the model is underfitting (capacity or training length); if
  train is much lower than val it is overfitting (regularisation, less
  capacity, early stopping).

## Watch for

- **Learning rates near or above 0.5 diverge** on this task. Proposing one is
  fine as a deliberate probe of the stability boundary, but say so in the
  rationale — do not stumble into it.
- **`seed` changes alone are not ideas.** They measure noise, not a
  hypothesis. If you want a variance estimate, say that explicitly.

## Output

Write `ideas.json` in the working directory:

```json
{"ideas": [
  {"name": "momentum-0.9",
   "rationale": "training is stable but slow to converge; momentum should reach a lower minimum in the same epochs",
   "parent_trial": null,
   "changes": {"momentum": 0.9}}
]}
```

`changes` is a convenience for this project: the implement phase applies it
to `config.json`. For an idea that needs real code edits, describe the change
in `rationale` and leave `changes` empty.

Base every rationale on results you actually read. Do not invent numbers.
