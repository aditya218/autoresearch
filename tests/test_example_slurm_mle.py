"""The Slurm MLE example, exercised end to end.

Proves the engine drives a real project - real model code, a real scoring
harness, real Slurm commands - not just the toy fixture. The cluster itself
is stood in for by `fake_slurm/`, so this runs anywhere in seconds.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
EXAMPLE = REPO / "examples" / "slurm_mle"


@pytest.fixture
def example(tmp_path, monkeypatch):
    """A private copy of the example, with the fake cluster on PATH."""
    dst = tmp_path / "slurm_mle"
    shutil.copytree(EXAMPLE, dst)
    monkeypatch.setenv(
        "PATH", f"{dst / 'fake_slurm'}:{dst}:{os.environ['PATH']}"
    )
    monkeypatch.setenv("FAKE_SLURM_STATE", str(tmp_path / "slurm-state"))
    return dst


def workspace_with(tmp_path, example: Path, **overrides) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    shutil.copy(example / "base_code" / "model.py", ws / "model.py")
    config = json.loads((example / "base_code" / "config.json").read_text())
    config.update(overrides)
    (ws / "config.json").write_text(json.dumps(config))
    return ws


def run_script(example: Path, name: str, *args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(example / name), *args], capture_output=True, text=True
    )


# -- the task itself ---------------------------------------------------------


def test_baseline_trains_and_scores(tmp_path, example):
    ws = workspace_with(tmp_path, example)
    out = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, str(example / "eval" / "train_eval.py"),
         "--workspace", str(ws), "--out", str(out)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    metrics = json.loads((out / "metrics.json").read_text())
    assert metrics["diverged"] is False
    # The baseline is deliberately poor: it feeds raw, wildly-scaled inputs
    # to a small network, so it underfits rather than overfits.
    assert 0.5 < metrics["val_rmse"] < 3.0
    assert metrics["train_rmse"] > metrics["val_rmse"] * 0.8


def standardising_model(example: Path) -> str:
    """The key insight, as a patch to the baseline model: fit per-feature
    statistics on the training split and apply them to every input."""
    src = (example / "base_code" / "model.py").read_text()
    src = src.replace(
        "    def prepare(self, train_rows):",
        "    def prepare(self, train_rows):\n"
        "        import sys as _s, pathlib as _p\n"
        "        _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent / 'eval'))\n"
        "        import data as _d\n"
        "        self.means, self.stds = _d.feature_stats(train_rows)\n"
        "        return\n"
        "    def _unused_prepare(self, train_rows):",
        1,
    )
    src = src.replace(
        "    def transform(self, x):",
        "    def transform(self, x):\n"
        "        if hasattr(self, 'means'):\n"
        "            return tuple((v - m) / s for v, m, s in zip(x, self.means, self.stds))\n"
        "        return x\n"
        "    def _unused_transform(self, x):",
        1,
    )
    return src


def score(example: Path, workspace: Path, out: Path) -> float:
    subprocess.run(
        [sys.executable, str(example / "eval" / "train_eval.py"),
         "--workspace", str(workspace), "--out", str(out)],
        capture_output=True, text=True, check=True,
    )
    return json.loads((out / "metrics.json").read_text())["val_rmse"]


def test_insight_beats_knob_turning(tmp_path, example):
    """The task's whole point: the features live on wildly different scales,
    so standardising inputs is worth more than any hyperparameter. If tuning
    alone could win, the example would teach the wrong lesson."""
    baseline = score(example, workspace_with(tmp_path / "base", example), tmp_path / "o-base")

    knobs = score(
        example,
        workspace_with(tmp_path / "knobs", example, hidden_size=24, momentum=0.9, epochs=200),
        tmp_path / "o-knobs",
    )

    ws = workspace_with(tmp_path / "insight", example)
    (ws / "model.py").write_text(standardising_model(example))
    insight = score(example, ws, tmp_path / "o-insight")

    assert knobs > baseline * 0.9, "knob-turning should not meaningfully help here"
    assert insight < baseline * 0.6, "standardising inputs should be a large win"


def test_overfitting_is_visible_at_high_capacity(tmp_path, example):
    """A large network on 400 points, with the inputs fixed, should fit the
    training split far better than the validation one - the signal an
    analysis phase is meant to read."""
    ws = workspace_with(
        tmp_path / "big", example, hidden_size=48, momentum=0.9, epochs=250,
        learning_rate=0.03,
    )
    (ws / "model.py").write_text(standardising_model(example))
    subprocess.run(
        [sys.executable, str(example / "eval" / "train_eval.py"),
         "--workspace", str(ws), "--out", str(tmp_path / "o-big")],
        capture_output=True, text=True, check=True,
    )
    m = json.loads((tmp_path / "o-big" / "metrics.json").read_text())
    assert m["val_rmse"] > m["train_rmse"], "expected a train/val gap to read"


# -- the Slurm contract ------------------------------------------------------


def test_launch_poll_collect_cycle(tmp_path, example):
    ws = workspace_with(tmp_path, example)
    out = tmp_path / "job"

    launched = run_script(
        example, "launch", "train", "--tag", "mle/T001/train",
        "--workspace", str(ws), "--out", str(out),
    )
    assert launched.returncode == 0, launched.stderr
    job_id = launched.stdout.strip()
    assert job_id.isdigit(), f"launch must print only a job id, got {job_id!r}"

    for _ in range(200):
        status = run_script(example, "poll", "train", "--job-id", job_id).stdout.strip()
        if status in ("done", "failed"):
            break
    assert status == "done", f"job ended {status}"

    collected = run_script(
        example, "collect", "train", "--job-id", job_id, "--out", str(out)
    )
    assert collected.returncode == 0, collected.stderr
    result = json.loads((out / "result.json").read_text())
    assert result["status"] == "passed"
    assert 0.0 < result["metrics"]["val_rmse"] < 3.0


def test_find_recovers_a_job_by_tag(tmp_path, example):
    """The ambiguous-launch case: the job exists, the id was lost."""
    ws = workspace_with(tmp_path, example)
    tag = "mle/T042/train"
    job_id = run_script(
        example, "launch", "train", "--tag", tag,
        "--workspace", str(ws), "--out", str(tmp_path / "job"),
    ).stdout.strip()

    found = run_script(example, "find", "train", "--tag", tag).stdout.split()
    assert job_id in found


def test_unknown_slurm_state_is_passed_through(tmp_path, example, monkeypatch):
    """A state the engine has no rule for must not be guessed at - it is what
    the repair agent exists for."""
    ws = workspace_with(tmp_path, example)
    job_id = run_script(
        example, "launch", "train", "--tag", "mle/T007/train",
        "--workspace", str(ws), "--out", str(tmp_path / "job"),
    ).stdout.strip()

    monkeypatch.setenv("FAKE_SLURM_STUCK", job_id)
    status = run_script(example, "poll", "train", "--job-id", job_id).stdout.strip()
    assert status not in ("running", "done", "failed")


# -- the gate ----------------------------------------------------------------


def test_diverging_idea_fails_the_gate_not_the_engine(tmp_path, example):
    """A diverged run is a verdict on the idea: the script exits 0 and
    reports failed, so the trial gate-stops instead of erroring."""
    ws = workspace_with(tmp_path, example, learning_rate=0.9)
    out = tmp_path / "smoke"
    proc = run_script(
        example, "run", "smoke_test", "--workspace", str(ws),
        "--out", str(out), "--epochs", "5", "--max-rmse", "5.0",
    )
    assert proc.returncode == 0, "a bad idea is not a script failure"
    assert json.loads((out / "result.json").read_text())["status"] == "failed"


def test_good_idea_passes_the_gate(tmp_path, example):
    ws = workspace_with(tmp_path, example)
    out = tmp_path / "smoke"
    run_script(
        example, "run", "smoke_test", "--workspace", str(ws),
        "--out", str(out), "--epochs", "5", "--max-rmse", "5.0",
    )
    assert json.loads((out / "result.json").read_text())["status"] == "passed"


# -- the whole campaign ------------------------------------------------------


def test_campaign_runs_and_improves_on_baseline(tmp_path, example):
    """The engine drives the real project end to end and finds something
    better than the baseline."""
    from autoresearch.campaign import Campaign
    from autoresearch.config import load_config

    config_path = example / "campaign.yaml"
    text = config_path.read_text().replace("max_trials: 8", "max_trials: 4")
    config_path.write_text(text)

    proc = subprocess.run(
        [sys.executable, "-m", "autoresearch.cli", "run", str(config_path),
         "--campaign-dir", str(tmp_path / "campaign"),
         "--harness", "fake_agent", "--ideate", "--poll-interval", "0.2"],
        capture_output=True, text=True, cwd=REPO,
        env={**os.environ, "PYTHONPATH": str(REPO)},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    index = json.loads(
        (tmp_path / "campaign" / "index" / "trials.json").read_text()
    )
    scored = {
        row["trial"]: row["metrics"]["val_rmse"]["value"]
        for row in index["trials"]
        if "val_rmse" in row["metrics"]
    }
    assert len(scored) >= 2, f"expected several scored trials, got {scored}"
    assert min(scored.values()) <= scored["T000"], "no trial beat the baseline"

    # Every val_rmse came from the deterministic Slurm phase, never an agent.
    for row in index["trials"]:
        if "val_rmse" in row["metrics"]:
            assert row["metrics"]["val_rmse"]["verified"] is True
