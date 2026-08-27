import json

import pytest

from autoresearch.cli import main


def test_validate_toy_config(capsys):
    assert main(["validate", "toy_project/campaign.yaml"]) == 0
    out = capsys.readouterr().out
    assert "config ok: toy" in out
    assert "implement -> smoke_test -> train -> analyze" in out
    assert "key metric:  score from train" in out


def test_validate_reports_bad_config(tmp_path, capsys):
    bad = tmp_path / "c.yaml"
    bad.write_text(
        "name: c\ngoal: g\nworkflow:\n  a: {agentic: true}\n"
        "key_metrics:\n  m: {from: a}\n"
    )
    assert main(["validate", str(bad)]) == 1
    assert "deterministic" in capsys.readouterr().err


def test_validate_reports_missing_scripts(tmp_path, toy_project, capsys):
    (toy_project / "poll").unlink()
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        f"name: c\ngoal: g\nproject_dir: {toy_project}\n"
        "workflow:\n  train: {uses: job}\n"
    )
    assert main(["validate", str(cfg)]) == 1
    assert "missing script" in capsys.readouterr().err


def test_run_phase_local(tmp_path, toy_project, workspace, capsys):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        f"name: c\ngoal: g\nproject_dir: {toy_project}\n"
        "workflow:\n  smoke_test: {uses: local, params: {scale: 0.1}}\n"
    )
    out = tmp_path / "phase"
    code = main([
        "run-phase", str(cfg), "smoke_test",
        "--workspace", str(workspace), "--out", str(out),
    ])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "passed"
    assert payload["metrics"]["score"] == pytest.approx(0.52)
    assert payload["verified"] is True


def test_run_phase_job(tmp_path, toy_project, workspace, capsys):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        f"name: c\ngoal: g\nproject_dir: {toy_project}\n"
        "workflow:\n  train: {uses: job, params: {polls: 1, scale: 1.0}}\n"
    )
    code = main([
        "run-phase", str(cfg), "train",
        "--workspace", str(workspace), "--out", str(tmp_path / "phase"),
        "--poll-interval", "0",
    ])
    assert code == 0
    out = capsys.readouterr().out
    assert "launched job toy-" in out
    assert json.loads(out[out.index("{"):])["metrics"]["score"] == pytest.approx(0.70)


def test_run_phase_rejects_agentic_for_now(capsys):
    code = main([
        "run-phase", "toy_project/campaign.yaml", "implement",
        "--workspace", ".", "--out", ".",
    ])
    assert code == 2
    assert "agentic phase" in capsys.readouterr().err


def test_run_phase_unknown_phase(capsys):
    code = main([
        "run-phase", "toy_project/campaign.yaml", "nope",
        "--workspace", ".", "--out", ".",
    ])
    assert code == 1
    assert "no phase" in capsys.readouterr().err
