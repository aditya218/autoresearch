import json

import pytest

from autoresearch.config import PhaseConfig
from autoresearch.phases import JobPhase, PhaseFailure, run_local_phase
from autoresearch.project import Project


def cfg(**kw) -> PhaseConfig:
    kw.setdefault("uses", "local")
    return PhaseConfig(**kw)


# -- local phases ------------------------------------------------------------


def test_local_phase_passes_and_reports_metrics(tmp_path, toy_project, workspace):
    out = tmp_path / "phase"
    outcome = run_local_phase(
        Project(toy_project), "smoke_test", cfg(params={"scale": 0.1}), workspace, out
    )
    assert outcome.status == "passed"
    assert outcome.metrics["score"] == pytest.approx(0.52)
    assert outcome.verified is True  # deterministic phase -> trusted
    assert (out / "raw_output.txt").exists()


def test_local_phase_failure_is_a_result_not_an_error(tmp_path, toy_project, workspace):
    outcome = run_local_phase(
        Project(toy_project), "smoke_test", cfg(params={"fail": "yes"}),
        workspace, tmp_path / "phase",
    )
    assert outcome.status == "failed"  # ran fine, reported failure (a gate stop)


def test_script_crash_is_a_phase_failure(tmp_path, toy_project, workspace):
    with pytest.raises(PhaseFailure):
        run_local_phase(
            Project(toy_project), "smoke_test", cfg(params={"crash": "yes"}),
            workspace, tmp_path / "phase",
        )


def test_missing_declared_output_fails_the_producer(tmp_path, toy_project, workspace):
    with pytest.raises(PhaseFailure, match="declared outputs"):
        run_local_phase(
            Project(toy_project), "smoke_test", cfg(produces=["ghost.json"]),
            workspace, tmp_path / "phase",
        )


def test_missing_script_is_a_phase_failure(tmp_path, toy_project, workspace):
    (toy_project / "run").unlink()
    with pytest.raises(PhaseFailure):
        run_local_phase(
            Project(toy_project), "smoke_test", cfg(), workspace, tmp_path / "phase"
        )


# -- job phases --------------------------------------------------------------


def drive(job: JobPhase, max_polls: int = 20) -> str:
    for _ in range(max_polls):
        status = job.poll()
        if job.finished:
            return status
    raise AssertionError(f"job never finished; last status {job.status!r}")


def make_job(toy_project, workspace, tmp_path, **params) -> JobPhase:
    params.setdefault("polls", 2)
    return JobPhase(
        project=Project(toy_project), phase="train",
        cfg=PhaseConfig(uses="job", params=params),
        workspace=workspace, phase_dir=tmp_path / "phase", tag="c/T001/train",
    )


def test_job_launch_poll_collect(tmp_path, toy_project, workspace):
    job = make_job(toy_project, workspace, tmp_path, scale=1.0)
    job_id = job.launch()
    assert job_id.startswith("toy-")
    assert drive(job) == "done"
    outcome = job.collect()
    assert outcome.status == "passed"
    assert outcome.metrics["score"] == pytest.approx(0.70)
    assert outcome.job_id == job_id


def test_job_failure_reported(tmp_path, toy_project, workspace):
    job = make_job(toy_project, workspace, tmp_path, outcome="failed")
    job.launch()
    assert drive(job) == "failed"
    outcome = job.collect()
    assert outcome.status == "failed"


def test_unknown_status_passes_through(tmp_path, toy_project, workspace):
    """A status the engine has no rule for is surfaced, not guessed at - it's
    what the repair agent is for."""
    job = make_job(toy_project, workspace, tmp_path, outcome="stuck", polls=0)
    job.launch()
    status = job.poll()
    assert status == "stuck"
    assert job.finished is False


def test_dead_launcher_is_a_phase_failure(tmp_path, toy_project, workspace):
    job = make_job(toy_project, workspace, tmp_path)
    job.cfg.params["launch-mode"] = "die"
    with pytest.raises(PhaseFailure):
        job.launch()


def test_ambiguous_launch_recovered_via_find(tmp_path, toy_project, workspace):
    """Launcher created a job then died before printing its id: `find`
    resolves it by tag instead of orphaning the job."""
    job = make_job(toy_project, workspace, tmp_path)
    job.cfg.params["launch-mode"] = "ambiguous"
    job_id = job.launch()
    assert job_id.startswith("toy-")
    assert drive(job) == "done"


def test_ambiguous_launch_without_find_fails(tmp_path, toy_project, workspace):
    (toy_project / "find").unlink()
    job = make_job(toy_project, workspace, tmp_path)
    job.cfg.params["launch-mode"] = "ambiguous"
    with pytest.raises(PhaseFailure):
        job.launch()


def test_poll_before_launch_rejected(tmp_path, toy_project, workspace):
    with pytest.raises(PhaseFailure, match="polled before launch"):
        make_job(toy_project, workspace, tmp_path).poll()
