"""A small MLP, in pure Python.

This is the code a trial edits. Everything here is fair game: the
architecture, the initialisation, the activation, the update rule, how
inputs are handled. The scoring code is deliberately *not* here - it lives
in the project's eval harness, outside the workspace, so a trial can change
the model but not the measurement.

The baseline is deliberately naive. It feeds raw inputs to a small network
and trains with plain SGD and no regularisation, which is a poor fit for
this data - see the task description in eval/data.py.
"""

import math
import random


def activation_fn(name):
    if name == "tanh":
        return math.tanh, lambda y: 1.0 - y * y
    if name == "relu":
        return (lambda x: x if x > 0 else 0.0), (lambda y: 1.0 if y > 0 else 0.0)
    if name == "sigmoid":
        sig = lambda x: 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, x))))
        return sig, lambda y: y * (1.0 - y)
    raise ValueError(f"unknown activation: {name}")


class MLP:
    """One hidden layer: n_in -> hidden -> 1."""

    def __init__(self, n_in, hidden_size, activation="tanh", weight_scale=0.5, seed=0):
        rng = random.Random(seed)
        self.act, self.dact = activation_fn(activation)
        self.n_in = n_in
        self.hidden_size = hidden_size
        self.w1 = [
            [rng.gauss(0.0, weight_scale) for _ in range(n_in)]
            for _ in range(hidden_size)
        ]
        self.b1 = [0.0] * hidden_size
        self.w2 = [rng.gauss(0.0, weight_scale) for _ in range(hidden_size)]
        self.b2 = 0.0
        self.vw1 = [[0.0] * n_in for _ in range(hidden_size)]
        self.vb1 = [0.0] * hidden_size
        self.vw2 = [0.0] * hidden_size
        self.vb2 = 0.0

    def prepare(self, train_rows):
        """Called once before training, with the training split only.

        The baseline does nothing here. It is the obvious place for anything
        that must be fitted on training data and then applied to every
        input - and fitting it on anything but `train_rows` would leak the
        validation set.
        """
        return

    def transform(self, x):
        """Applied to every input, in training and in evaluation alike.

        The baseline passes inputs through untouched.
        """
        return x

    def forward(self, x):
        x = self.transform(x)
        h = []
        for j in range(self.hidden_size):
            z = self.b1[j] + sum(self.w1[j][i] * x[i] for i in range(len(x)))
            h.append(self.act(z))
        y = self.b2 + sum(self.w2[j] * h[j] for j in range(self.hidden_size))
        return y, h

    def train_batch(self, batch, lr, momentum, weight_decay=0.0):
        """One SGD step (with optional momentum) over a minibatch."""
        n = len(batch)
        gw1 = [[0.0] * self.n_in for _ in range(self.hidden_size)]
        gb1 = [0.0] * self.hidden_size
        gw2 = [0.0] * self.hidden_size
        gb2 = 0.0

        for raw_x, target in batch:
            y, h = self.forward(raw_x)
            x = self.transform(raw_x)
            dy = 2.0 * (y - target) / n
            gb2 += dy
            for j in range(self.hidden_size):
                gw2[j] += dy * h[j]
                dh = dy * self.w2[j] * self.dact(h[j])
                gb1[j] += dh
                for i in range(self.n_in):
                    gw1[j][i] += dh * x[i]

        for j in range(self.hidden_size):
            gw2[j] += weight_decay * self.w2[j]
            self.vw2[j] = momentum * self.vw2[j] - lr * gw2[j]
            self.w2[j] += self.vw2[j]
            self.vb1[j] = momentum * self.vb1[j] - lr * gb1[j]
            self.b1[j] += self.vb1[j]
            for i in range(self.n_in):
                gw1[j][i] += weight_decay * self.w1[j][i]
                self.vw1[j][i] = momentum * self.vw1[j][i] - lr * gw1[j][i]
                self.w1[j][i] += self.vw1[j][i]
        self.vb2 = momentum * self.vb2 - lr * gb2
        self.b2 += self.vb2


def build(config, n_in):
    return MLP(
        n_in=n_in,
        hidden_size=config["hidden_size"],
        activation=config["activation"],
        weight_scale=config["weight_scale"],
        seed=config["seed"],
    )
