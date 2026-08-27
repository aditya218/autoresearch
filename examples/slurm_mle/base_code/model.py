"""A small MLP, in pure Python.

This is the code a trial edits. Everything here is fair game for an idea:
the architecture, the initialisation, the activation, the update rule. The
scoring code is deliberately *not* here - it lives in the project's eval
harness, outside the workspace, so a trial can change the model but not the
measurement.
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
    """Two-layer network: n_in -> hidden -> 1."""

    def __init__(self, n_in, hidden_size, activation="tanh", weight_scale=0.5, seed=0):
        rng = random.Random(seed)
        self.act, self.dact = activation_fn(activation)
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

    def forward(self, x):
        h = []
        for j in range(self.hidden_size):
            z = self.b1[j] + sum(self.w1[j][i] * x[i] for i in range(len(x)))
            h.append(self.act(z))
        y = self.b2 + sum(self.w2[j] * h[j] for j in range(self.hidden_size))
        return y, h

    def train_batch(self, batch, lr, momentum):
        """One SGD step (with optional momentum) over a minibatch."""
        n = len(batch)
        gw1 = [[0.0] * len(batch[0][0]) for _ in range(self.hidden_size)]
        gb1 = [0.0] * self.hidden_size
        gw2 = [0.0] * self.hidden_size
        gb2 = 0.0

        for x, target in batch:
            y, h = self.forward(x)
            dy = 2.0 * (y - target) / n
            gb2 += dy
            for j in range(self.hidden_size):
                gw2[j] += dy * h[j]
                dh = dy * self.w2[j] * self.dact(h[j])
                gb1[j] += dh
                for i in range(len(x)):
                    gw1[j][i] += dh * x[i]

        for j in range(self.hidden_size):
            self.vw2[j] = momentum * self.vw2[j] - lr * gw2[j]
            self.w2[j] += self.vw2[j]
            self.vb1[j] = momentum * self.vb1[j] - lr * gb1[j]
            self.b1[j] += self.vb1[j]
            for i in range(len(gw1[j])):
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
