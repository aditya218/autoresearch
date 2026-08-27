"""Trial-level tests: the workflow walk, gates, retries, provenance, resume."""

import asyncio
import json
from pathlib import Path

import pytest

from autoresearch.campaign import Campaign
from autoresearch.contract import PhaseResult, write_result
from autoresearch.engine import run_trial
from autoresearch.phases import PhaseOutcome, finalize

BASE_CONFIG = """
name: t
goal: g
project_dir: {project}
{key_metrics}workflow:
{workflow}
"""


def write_config(
    tmp_path: Path, project: Path, workflow: str, key_metric_phase: str | None = "train"
) -> Path:
    """`key_metric_phase` binds `score` to a phase (§7.3); pass None for
    workflows that have no such phase."""
    key_metrics = (
        f"key_metrics:\n  score: {{from: {key_metric_phase}}}\n"
        if key_metric_phase and key_metric_phase in workflow
        else ""
    )
    path = tmp_path / "campaign.yaml"
    path.write_text(
        BASE_CONFIG.format(project=project, workflow=workflow, key_metrics=key_metrics)
    )
    return path


def agent_stub(delta: float = 0.2, status: str = "passed", metrics: dict | None = None):
    """Stands in for the agent harness: writes change.json into the workspace
    and reports a result."""

    def run(phase, cfg, workspace: Path, phase_dir: Path) -> PhaseOutcome:
        (workspace / "change.json").write_text(
            json.dumps({"name": "idea", "delta": delta})
        )
        write_result(
            phase_dir,
            PhaseResult(status=status, metrics=metrics or {}, notes=f"agent {phase}"),
        )
        return finalize(phase_dir, cfg, verified=False, workspace=workspace)

    return run


def start(campaign: Campaign, trial_id: str = "T001") -> str:
    campaign.create_trial(trial_id)
    campaign.prepare_workspace(trial_id)
    return trial_id


def drive(campaign: Campaign, trial_id: str, run_agentic=None) -> str:
    ctx = campaign.trial_context(
        trial_id, poll_interval=0, run_agentic=run_agentic or agent_stub()
    )
    return asyncio.run(run_trial(ctx))


# -- happy path --------------------------------------------------------------

FULL_WORKFLOW = """
  implement: {agentic: true, skills: [s], produces: [change.json]}
  smoke_test: {after: implement, gate: true, uses: local, params: {scale: 0.1}}
  train: {after: smoke_test, uses: job, params: {polls: 1, scale: 1.0}}
  analyze: {after: train, agentic: true, skills: [a]}
"""


def test_full_workflow_completes(tmp_path, toy_project):
    cfg = write_config(tmp_path, toy_project, FULL_WORKFLOW)
    with Campaign(tmp_path / "campaign", cfg) as c:
        t = start(c)
        assert drive(c, t) == "completed"
        trial = c.state.trials[t]
        assert [p.status for p in trial.phases.values()] == ["passed"] * 4
        assert trial.metrics["score"].value == pytest.approx(0.70)
        assert trial.metrics["score"].verified is True
        assert trial.metrics["score"].phase == "train"


def test_views_current_after_run(tmp_path, toy_project):
    cfg = write_config(tmp_path, toy_project, FULL_WORKFLOW)
    with Campaign(tmp_path / "campaign", cfg) as c:
        t = start(c)
        drive(c, t)
        view = json.loads((tmp_path / "campaign" / "trials" / t / "trial.json").read_text())
        assert view["status"] == "completed"
        assert view["phases"]["train"]["job_id"].startswith("toy-")
        index = json.loads((tmp_path / "campaign" / "index" / "trials.json").read_text())
        assert index["trials"][0]["status"] == "completed"


# -- gates -------------------------------------------------------------------


def test_failed_gate_stops_trial_before_expensive_phase(tmp_path, toy_project):
    workflow = """
  implement: {agentic: true, skills: [s]}
  smoke_test: {after: implement, gate: true, uses: local, params: {fail: yes}}
  train: {after: smoke_test, uses: job}
"""
    cfg = write_config(tmp_path, toy_project, workflow)
    with Campaign(tmp_path / "campaign", cfg) as c:
        t = start(c)
        assert drive(c, t) == "gate_stopped"
        trial = c.state.trials[t]
        assert "train" not in trial.phases  # never launched
        assert trial.reason == "gate smoke_test failed"


def test_non_gate_failure_is_failed_idea(tmp_path, toy_project):
    workflow = """
  implement: {agentic: true, skills: [s]}
  train: {after: implement, uses: job, params: {polls: 0, outcome: failed}}
"""
    cfg = write_config(tmp_path, toy_project, workflow)
    with Campaign(tmp_path / "campaign", cfg) as c:
        t = start(c)
        assert drive(c, t) == "failed_idea"


# -- retries and infra errors ------------------------------------------------


def test_infra_error_retries_then_errors_trial(tmp_path, toy_project):
    workflow = """
  implement: {agentic: true, skills: [s]}
  smoke_test: {after: implement, uses: local, params: {crash: yes}, max_retries: 2}
"""
    cfg = write_config(tmp_path, toy_project, workflow)
    with Campaign(tmp_path / "campaign", cfg) as c:
        t = start(c)
        assert drive(c, t) == "errored"
        attempts = [
            e for e in c.ledger.events()
            if e.type == "phase_errored" and e.phase == "smoke_test"
        ]
        assert len(attempts) == 3  # initial + 2 retries
        assert c.state.budget_consumed_trials == 0  # infra errors are free


def test_missing_declared_output_errors_the_producer(tmp_path, toy_project):
    workflow = """
  implement: {agentic: true, skills: [s], produces: [never_written.json]}
"""
    cfg = write_config(tmp_path, toy_project, workflow)
    with Campaign(tmp_path / "campaign", cfg) as c:
        t = start(c)
        assert drive(c, t) == "errored"


def test_agentic_phase_without_harness_errors(tmp_path, toy_project):
    cfg = write_config(tmp_path, toy_project, FULL_WORKFLOW)
    with Campaign(tmp_path / "campaign", cfg) as c:
        t = start(c)
        ctx = c.trial_context(t, poll_interval=0, run_agentic=None)
        assert asyncio.run(run_trial(ctx)) == "errored"


# -- metric provenance -------------------------------------------------------


def test_agentic_phase_cannot_report_a_key_metric(tmp_path, toy_project):
    """`score` is bound to `train`; an agent claiming it is dropped (§7.3)."""
    workflow = """
  implement: {agentic: true, skills: [s]}
  train: {after: implement, uses: job, params: {polls: 0, scale: 1.0}}
"""
    cfg = write_config(tmp_path, toy_project, workflow)
    with Campaign(tmp_path / "campaign", cfg) as c:
        t = start(c)
        drive(c, t, run_agentic=agent_stub(metrics={"score": 0.99}))
        trial = c.state.trials[t]
        assert trial.metrics["score"].value == pytest.approx(0.70)  # from train
        assert trial.metrics["score"].verified is True


def test_agentic_phase_may_report_other_numbers_unverified(tmp_path, toy_project):
    workflow = """
  implement: {agentic: true, skills: [s]}
  train: {after: implement, uses: job, params: {polls: 0}}
"""
    cfg = write_config(tmp_path, toy_project, workflow)
    with Campaign(tmp_path / "campaign", cfg) as c:
        t = start(c)
        drive(c, t, run_agentic=agent_stub(metrics={"agent_guess": 0.42}))
        guess = c.state.trials[t].metrics["agent_guess"]
        assert guess.value == pytest.approx(0.42)
        assert guess.verified is False


# -- resume ------------------------------------------------------------------


def test_completed_phases_are_not_rerun_on_resume(tmp_path, toy_project):
    cfg = write_config(tmp_path, toy_project, FULL_WORKFLOW)
    campaign_dir = tmp_path / "campaign"
    with Campaign(campaign_dir, cfg) as c:
        t = start(c)
        drive(c, t)

    # Reopen and walk the same trial again: everything already passed, so no
    # new phase attempts are recorded.
    with Campaign(campaign_dir, cfg) as c:
        before = sum(1 for e in c.ledger.events() if e.type == "phase_started")
        ctx = c.trial_context(t, poll_interval=0, run_agentic=agent_stub())
        asyncio.run(run_trial(ctx))
        after = sum(1 for e in c.ledger.events() if e.type == "phase_started")
    assert after == before


def test_resume_reattaches_running_job_instead_of_relaunching(tmp_path, toy_project):
    """Mid-workflow resume: a job already launched is polled, not launched
    again - the never-lose-a-job invariant."""
    workflow = """
  train: {uses: job, params: {polls: 3, scale: 1.0}}
"""
    cfg = write_config(tmp_path, toy_project, workflow)
    campaign_dir = tmp_path / "campaign"

    with Campaign(campaign_dir, cfg) as c:
        t = start(c)
        # The job snapshots its inputs at launch, so seed the workspace first.
        (c.workspace_dir(t) / "change.json").write_text(
            json.dumps({"name": "idea", "delta": 0.2})
        )
        # Simulate a crash right after launch: record the launch, stop there.
        from autoresearch import events as ev
        from autoresearch.phases import JobPhase
        from autoresearch.config import PhaseConfig

        c.recorder.record(ev.PhaseStarted, trial=t, phase="train", attempt=1)
        job = JobPhase(
            project=c.project, phase="train",
            cfg=PhaseConfig(uses="job", params={"polls": 3, "scale": 1.0}),
            workspace=c.workspace_dir(t), phase_dir=c.dir / "trials" / t / "phases" / "train",
            tag=f"t/{t}/train",
        )
        job_id = job.launch()
        c.recorder.record(
            ev.JobLaunched, trial=t, phase="train", job_id=job_id, tag=job.tag
        )

    with Campaign(campaign_dir, cfg) as c:
        assert c.report.reattached_jobs == [job_id]
        status = drive(c, t)
        assert status == "completed"
        launched = [e for e in c.ledger.events() if e.type == "job_launched"]
        assert len(launched) == 1  # never relaunched
        assert c.state.trials[t].metrics["score"].value == pytest.approx(0.70)
