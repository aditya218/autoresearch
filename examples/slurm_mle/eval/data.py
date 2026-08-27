"""The task: a fixed synthetic regression problem.

Lives with the eval harness, outside the workspace, so every trial is scored
on exactly the same data. Changing the model is the experiment; changing the
data would be moving the goalposts.
"""

import math
import random

N_FEATURES = 2


def target_fn(x1, x2):
    return math.sin(2.5 * x1) + 0.5 * x2 * x2 - 0.3 * x1 * x2


def make_split(n, seed, noise=0.05):
    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        x1 = rng.uniform(-2.0, 2.0)
        x2 = rng.uniform(-2.0, 2.0)
        y = target_fn(x1, x2) + rng.gauss(0.0, noise)
        rows.append(((x1, x2), y))
    return rows


def load(train_n=600, val_n=200):
    """Fixed seeds: the same train/val split for every trial, always."""
    return make_split(train_n, seed=1234), make_split(val_n, seed=99)
