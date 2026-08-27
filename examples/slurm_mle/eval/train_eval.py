#!/usr/bin/env python3
"""Train the workspace's model and score it. The engine's trusted metric.

Imports `model.py` and `config.json` from the trial's workspace, trains on
the fixed dataset, and writes `metrics.json`. A trial can change the model
however it likes; it cannot change how the result is measured, because this
file is not in the workspace.
"""

import argparse
import importlib.util
import json
import math
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import data  # noqa: E402


def load_workspace_model(workspace: Path):
    spec = importlib.util.spec_from_file_location(
        "trial_model", workspace / "model.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rmse(model, rows) -> float:
    total = 0.0
    for x, target in rows:
        y, _ = model.forward(x)
        total += (y - target) ** 2
    return math.sqrt(total / len(rows))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--epochs", type=int, default=None, help="override for smoke tests")
    args = ap.parse_args()

    workspace = Path(args.workspace).resolve()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    config = json.loads((workspace / "config.json").read_text())
    if args.epochs is not None:
        config["epochs"] = args.epochs

    model_module = load_workspace_model(workspace)
    train_rows, val_rows = data.load()
    model = model_module.build(config, data.N_FEATURES)

    started = time.time()
    rng = random.Random(config["seed"])
    batch_size = max(1, int(config["batch_size"]))
    history = []
    for epoch in range(int(config["epochs"])):
        rng.shuffle(train_rows)
        for i in range(0, len(train_rows), batch_size):
            model.train_batch(
                train_rows[i : i + batch_size],
                lr=config["learning_rate"],
                momentum=config.get("momentum", 0.0),
            )
        if epoch % 10 == 0 or epoch == int(config["epochs"]) - 1:
            history.append({"epoch": epoch, "val_rmse": round(rmse(model, val_rows), 5)})

    val_rmse = rmse(model, val_rows)
    train_rmse = rmse(model, train_rows)
    elapsed = time.time() - started

    if not math.isfinite(val_rmse):
        # Diverged: a real outcome for a bad idea, not an infrastructure fault.
        val_rmse = float("inf")

    (out / "metrics.json").write_text(
        json.dumps(
            {
                "val_rmse": None if math.isinf(val_rmse) else round(val_rmse, 5),
                "train_rmse": None if math.isinf(train_rmse) else round(train_rmse, 5),
                "diverged": math.isinf(val_rmse),
                "seconds": round(elapsed, 2),
                "config": config,
                "history": history,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"val_rmse={val_rmse:.5f} train_rmse={train_rmse:.5f} in {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
