"""Campaign-loop tests: baseline, admission, budgets, drain, resume, controls."""

import asyncio
import json
from pathlib import Path

import pytest

from autoresearch.campaign import Campaign
from autoresearch.contract import PhaseResult, write_result
from autoresearch.loop import BASELINE_TRIAL, CampaignLoop
from autoresearch.phases import finalize

CONFIG = """
name: t
goal: g
project_dir: {project}
key_metrics:
  score: {{from: train}}
ideation:
  backlog_target: {backlog}
budget:
  max_trials: {max_trials}
  active_trials: {active_trials}
workflow:
  implement: {{agentic: true, skills: [s], produces: [change.json]}}
  train: {{after: implement, uses: job, params: {{polls: 0, scale: 1.0}}}}
"""


def write_config(tmp_path, project, max_trials=3, active_trials=2, backlog=2) -> Path:
    path = tmp_path / "campaign.yaml"
    path.write_text(
        CONFIG.format(
            project=project, max_trials=max_trials,
            active_trials=active_trials, backlog=backlog,
        )
    )
    return path


def agent_stub(phase, cfg, workspace: Path, phase_dir: Path):
    """Reads the idea the loop staged in the workspace and 'implements' it."""
    idea_file = workspace / "idea.json"
    idea = json.loads(idea_file.read_text()) if idea_file.exists() else {}
    (workspace / "change.json").write_text(
        json.dumps({"name": idea.get("name", "baseline"), "delta": idea.get("delta", 0.0)})
    )
    write_result(phase_dir, PhaseResult(status="passed", notes=f"stub {phase}"))
    return finalize(phase_dir, cfg, verified=False, workspace=workspace)


def counting_ideator(deltas=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6)):
    """Hands out ideas from a fixed list; records how often it was asked."""
    state = {"calls": 0, "n": 0}

    def ideate(campaign, wanted):
        state["calls"] += 1
        out = []
        for _ in range(wanted):
            i = state["n"]
            state["n"] += 1
            out.append({"name": f"idea-{i}", "delta": deltas[i % len(deltas)]})
        return out

    ideate.state = state
    return ideate


def make_loop(tmp_path, toy_project, **cfg_kw) -> CampaignLoop:
    cfg = write_config(tmp_path, toy_project, **cfg_kw)
    campaign = Campaign(tmp_path / "campaign", cfg)
    return CampaignLoop(
        campaign, ideator=counting_ideator(), run_agentic=agent_stub, poll_interval=0
    )


# -- baseline ----------------------------------------------------------------


def test_baseline_runs_first_and_is_the_reference(tmp_path, toy_project):
    loop = make_loop(tmp_path, toy_project, max_trials=1)
    with loop.c:
        result = asyncio.run(loop.run())
        assert result.status == "budget_reached"
        baseline = loop.c.state.trials[BASELINE_TRIAL]
        assert baseline.status == "completed"
        assert baseline.metrics["score"].value == pytest.approx(0.50)  # no change


def test_failed_baseline_halts_campaign(tmp_path, toy_project):
    """A broken workflow dies at T000 instead of burning the budget."""
    cfg = write_config(tmp_path, toy_project)
    (toy_project / "launch").write_text("#!/bin/sh\nexit 1\n")
    (toy_project / "launch").chmod(0o755)
    with Campaign(tmp_path / "campaign", cfg) as c:
        loop = CampaignLoop(c, ideator=counting_ideator(), run_agentic=agent_stub, poll_interval=0)
        result = asyncio.run(loop.run())
        assert result.status == "baseline_failed"
        assert c.state.finish_reason == "baseline_failed"
        # No ordinary trial was ever admitted.
        assert set(c.state.trials) == {BASELINE_TRIAL}


# -- admission and budgets ---------------------------------------------------


def test_campaign_stops_at_max_trials(tmp_path, toy_project):
    loop = make_loop(tmp_path, toy_project, max_trials=4, active_trials=2)
    with loop.c:
        result = asyncio.run(loop.run())
        assert result.status == "budget_reached"
        state = loop.c.state
        assert state.budget_consumed_trials == 4  # baseline + 3, never more
        assert state.status == "finished"
        assert state.finish_reason == "budget_reached"


def test_active_trials_caps_concurrency(tmp_path, toy_project):
    """No more than `active_trials` trials are ever in flight at once."""
    loop = make_loop(tmp_path, toy_project, max_trials=6, active_trials=2)
    peak = {"n": 0}
    original = loop._drive

    async def watched(trial_id):
        peak["n"] = max(peak["n"], loop.c.state.in_flight_trials)
        return await original(trial_id)

    loop._drive = watched
    with loop.c:
        asyncio.run(loop.run())
    assert peak["n"] <= 2


def test_ideator_keeps_backlog_topped_up(tmp_path, toy_project):
    loop = make_loop(tmp_path, toy_project, max_trials=4, backlog=2)
    with loop.c:
        asyncio.run(loop.run())
        assert loop.ideator.state["calls"] >= 1
        # Every admitted trial consumed exactly one idea.
        consumed = [i for i in loop.c.state.ideas.values() if i.consumed_by]
        assert len(consumed) == len(loop.c.state.trials) - 1  # baseline has no idea


def test_ideas_reach_trials_and_produce_different_scores(tmp_path, toy_project):
    loop = make_loop(tmp_path, toy_project, max_trials=3, active_trials=1)
    with loop.c:
        asyncio.run(loop.run())
        scores = {
            t.trial: t.metrics["score"].value
            for t in loop.c.state.trials.values()
            if "score" in t.metrics
        }
    assert scores[BASELINE_TRIAL] == pytest.approx(0.50)
    assert len(set(scores.values())) > 1  # ideas actually changed the outcome


def test_broken_ideator_stops_cleanly_instead_of_spinning(tmp_path, toy_project):
    """Ideation failures never crash a campaign, but with nothing left to run
    the loop stops for a human rather than spinning forever."""

    def broken(campaign, wanted):
        raise RuntimeError("ideator exploded")

    cfg = write_config(tmp_path, toy_project, max_trials=3)
    with Campaign(tmp_path / "campaign", cfg) as c:
        loop = CampaignLoop(c, ideator=broken, run_agentic=agent_stub, poll_interval=0)
        result = asyncio.run(loop.run())
        assert result.status == "stalled_ideation"
        assert c.state.trials[BASELINE_TRIAL].status == "completed"  # baseline ran
        assert c.state.status == "finished"
        failures = [
            e for e in c.ledger.events()
            if e.type == "campaign_paused" and "ideation failed" in e.reason
        ]
        assert len(failures) == loop.max_ideation_failures


def test_ideation_failure_does_not_stop_running_trials(tmp_path, toy_project):
    """A failing ideator drains the backlog; work already admitted finishes."""
    calls = {"n": 0}

    def flaky(campaign, wanted):
        calls["n"] += 1
        if calls["n"] == 1:
            return [{"name": "first", "delta": 0.2}]
        raise RuntimeError("ideator exploded")

    cfg = write_config(tmp_path, toy_project, max_trials=5)
    with Campaign(tmp_path / "campaign", cfg) as c:
        loop = CampaignLoop(c, ideator=flaky, run_agentic=agent_stub, poll_interval=0)
        result = asyncio.run(loop.run())
        assert result.status == "stalled_ideation"
        ordinary = [t for t in c.state.trials.values() if t.trial != BASELINE_TRIAL]
        assert len(ordinary) == 1
        assert ordinary[0].status == "completed"  # ran to completion regardless


# -- human in the loop -------------------------------------------------------


def test_injected_ideas_run_without_an_ideator(tmp_path, toy_project):
    """Ideation dialled to zero: the engine still runs the ideas you bring."""
    cfg = write_config(tmp_path, toy_project, max_trials=3)
    with Campaign(tmp_path / "campaign", cfg) as c:
        loop = CampaignLoop(c, ideator=None, run_agentic=agent_stub, poll_interval=0)
        loop.inject_idea(payload={"name": "mine", "delta": 0.4})
        result = asyncio.run(loop.run())
        assert result.status in {"budget_reached", "stopped"}
        trials = [t for t in c.state.trials.values() if t.trial != BASELINE_TRIAL]
        assert len(trials) == 1
        assert trials[0].metrics["score"].value == pytest.approx(0.90)


def test_pause_blocks_admission(tmp_path, toy_project):
    loop = make_loop(tmp_path, toy_project, max_trials=5)
    with loop.c:
        loop.pause("operator")
        assert loop.can_admit() is False
        loop.resume()
        loop.top_up_backlog()
        assert loop.can_admit() is True


def test_killed_trial_does_not_consume_budget(tmp_path, toy_project):
    loop = make_loop(tmp_path, toy_project, max_trials=5)
    with loop.c:
        loop.c.create_trial("T009")
        loop.kill_trial("T009")
        trial = loop.c.state.trials["T009"]
        assert trial.status == "killed"
        assert loop.c.state.budget_consumed_trials == 0


# -- resume ------------------------------------------------------------------


def test_raising_budget_and_restarting_continues(tmp_path, toy_project):
    """Resume = restart: raise max_trials, reopen, keep going (§8)."""
    cfg = write_config(tmp_path, toy_project, max_trials=2)
    with Campaign(tmp_path / "campaign", cfg) as c:
        loop = CampaignLoop(c, ideator=counting_ideator(), run_agentic=agent_stub, poll_interval=0)
        asyncio.run(loop.run())
        first_count = c.state.budget_consumed_trials
    assert first_count == 2

    write_config(tmp_path, toy_project, max_trials=4)
    with Campaign(tmp_path / "campaign", cfg) as c:
        assert c.state.finish_reason == "budget_reached"  # replayed from the log
        loop = CampaignLoop(c, ideator=counting_ideator(), run_agentic=agent_stub, poll_interval=0)
        loop.resume()
        asyncio.run(loop.run())
        assert c.state.budget_consumed_trials == 4
        assert c.state.trials[BASELINE_TRIAL].status == "completed"


# -- resuming after the engine died, with nothing left to admit --------------


def test_in_flight_trials_finish_even_when_the_budget_is_used_up(tmp_path, toy_project):
    """The engine dies while jobs are running; by the time it comes back
    there is nothing new to admit. The jobs kept running regardless, so the
    trials that own them must still be driven to completion - otherwise
    their results are stranded on the cluster."""
    from autoresearch import events as ev
    from autoresearch.config import PhaseConfig
    from autoresearch.phases import JobPhase

    cfg = write_config(tmp_path, toy_project, max_trials=2, active_trials=2)
    campaign_dir = tmp_path / "campaign"

    # First run: baseline completes, then a trial launches its job and the
    # process dies before the job finishes.
    with Campaign(campaign_dir, cfg) as c:
        loop = CampaignLoop(c, ideator=None, run_agentic=agent_stub, poll_interval=0)
        asyncio.run(loop.run_baseline())
        assert c.state.trials[BASELINE_TRIAL].status == "completed"

        loop.inject_idea(payload={"name": "mine", "delta": 0.3})
        trial_id = loop.admit(c.state.backlog[0])
        (c.workspace_dir(trial_id) / "change.json").write_text(
            json.dumps({"name": "mine", "delta": 0.3})
        )
        c.recorder.record(ev.PhaseStarted, trial=trial_id, phase="implement")
        c.recorder.record(
            ev.PhaseCompleted, trial=trial_id, phase="implement", status="passed"
        )
        c.recorder.record(ev.PhaseStarted, trial=trial_id, phase="train")
        job = JobPhase(
            project=c.project, phase="train",
            cfg=PhaseConfig(uses="job", params={"polls": 1, "scale": 1.0}),
            workspace=c.workspace_dir(trial_id),
            phase_dir=c.dir / "trials" / trial_id / "phases" / "train",
            tag=f"t/{trial_id}/train",
        )
        job_id = job.launch()
        c.recorder.record(
            ev.JobLaunched, trial=trial_id, phase="train", job_id=job_id, tag=job.tag
        )
        # <- process dies here

    # Restart. The budget is fully accounted for: one terminal trial plus one
    # in flight equals max_trials, so nothing new can be admitted.
    with Campaign(campaign_dir, cfg) as c:
        assert c.report.reattached_jobs == [job_id]
        loop = CampaignLoop(c, ideator=None, run_agentic=agent_stub, poll_interval=0)
        assert loop.can_admit() is False, "nothing should be admissible"
        assert loop.budget_exhausted() is True

        result = asyncio.run(loop.run())

        trial = c.state.trials[trial_id]
        assert trial.status == "completed", "the in-flight trial was abandoned"
        assert trial.metrics["score"].value == pytest.approx(0.80)
        # and it reattached rather than launching a second job
        launched = [
            e for e in c.ledger.events()
            if e.type == "job_launched" and e.trial == trial_id
        ]
        assert len(launched) == 1, "the resumed trial relaunched its job"
        assert result.status == "budget_reached"
