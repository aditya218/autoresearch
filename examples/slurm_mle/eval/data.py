"""The task: a synthetic regression problem with real structure.

Designed so that turning knobs plateaus and understanding the data pays.
Three properties do that work:

  - **Features live on wildly different scales.** x3 spans hundreds, x0
    spans ones. A network fed raw inputs wastes most of its capacity
    learning to undo that, so standardising inputs is worth more than any
    hyperparameter.
  - **The training set is small relative to the function's complexity.**
    400 points for a 5-D nonlinear target with interactions, so capacity
    without regularisation overfits and the val/train gap is informative.
  - **Two of the five features are pure noise.** They offer nothing, and a
    model with no weight decay will happily fit them.

The data and the split are fixed for every trial: changing the model is the
experiment, changing the measurement would be moving the goalposts.
"""

import math
import random

N_FEATURES = 5

#: deliberately unequal - raw inputs are a bad idea here
FEATURE_SCALES = (1.0, 3.0, 0.2, 120.0, 8.0)


def target_fn(x):
    """Nonlinear, with an interaction term. x[3] and x[4] are decoys."""
    x0, x1, x2, x3, x4 = x
    return (
        math.sin(1.8 * x0)
        + 0.6 * (x1 / 3.0) ** 2
        - 0.7 * (x0 * x1 / 3.0)          # interaction: needs a hidden layer
        + 1.2 * math.tanh(x2 / 0.2)
    )


def make_split(n, seed, noise=0.12):
    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        x = tuple(rng.gauss(0.0, s) for s in FEATURE_SCALES)
        y = target_fn(x) + rng.gauss(0.0, noise)
        rows.append((x, y))
    return rows


def load(train_n=400, val_n=400):
    """Fixed seeds: the same train/val split for every trial, always.

    The validation set is as large as the training set on purpose - the
    scarcity is in what the model learns from, not in how well it is
    measured, so a val_rmse difference means something.
    """
    return make_split(train_n, seed=1234), make_split(val_n, seed=99)


def feature_stats(rows):
    """Per-feature mean and standard deviation of a split.

    Provided because standardising inputs is the single biggest win on this
    task, and a trial should not have to reimplement it from scratch - but
    it must compute the statistics from the *training* split only, or it
    leaks the validation set into the model.
    """
    n = len(rows)
    means, stds = [], []
    for i in range(N_FEATURES):
        column = [row[0][i] for row in rows]
        mean = sum(column) / n
        var = sum((v - mean) ** 2 for v in column) / n
        means.append(mean)
        stds.append(math.sqrt(var) or 1.0)
    return means, stds
