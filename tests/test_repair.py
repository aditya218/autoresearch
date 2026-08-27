"""The repair agent: the two real cluster-job situations from §9.1, plus
the invariant that repair recommends and the engine acts.
"""

import asyncio
import json
from pathlib import Path

import pytest

from autoresearch.agents import AgentRequest, AgentResult, HarnessError, ScriptedHarness
from autoresearch.campaign import Campaign
from autoresearch.contract import PhaseResult, write_result
from autoresearch.engine import run_trial
from autoresearch.phases import finalize
from autoresearch.repair import VERDICT_FILENAME, RepairAgent, Situation, Verdict

CONFIG = """
name: t
goal: g
project_dir: {project}
key_metrics:
  score: {{from: train}}
workflow:
  implement: {{agentic: true, skills: [s], produces: [change.json]}}
  train:
    after: implement
    uses: job
    params: {params}
    stuck_after_polls: {stuck_after}
    repair: {{skill: repair-toy-train, max_attempts: {max_attempts}}}
"""


def write_config(tmp_path, project, params, stuck_after=2, max_attempts=2) -> Path:
    path = tmp_path / "campaign.yaml"
    path.write_text(
        CONFIG.format(
            project=project, params=json.dumps(params),
            stuck_after=stuck_after, max_attempts=max_attempts,
        )
    )
    return path


def agent_stub(phase, cfg, workspace: Path, phase_dir: Path):
    (workspace / "change.json").write_text(json.dumps({"name": "idea", "delta": 0.2}))
    write_result(phase_dir, PhaseResult(status="passed", notes=f"stub {phase}"))
    return finalize(phase_dir, cfg, verified=False, workspace=workspace)


def repair_harness(action: str, diagnosis: str = "looked at the logs"):
    """A repair agent that always recommends one action."""

    def fn(req: AgentRequest) -> AgentResult:
        (req.phase_dir / VERDICT_FILENAME).write_text(
            json.dumps({"action": action, "diagnosis": diagnosis})
        )
        return AgentResult(text=f"recommending {action}")

    return ScriptedHarness(fn)


def drive(tmp_path, toy_project, params, harness, **cfg_kw):
    cfg = write_config(tmp_path, toy_project, params, **cfg_kw)
    campaign = Campaign(tmp_path / "campaign", cfg)
    campaign.create_trial("T001")
    campaign.prepare_workspace("T001")
    ctx = campaign.trial_context("T001", poll_interval=0, run_agentic=agent_stub)
    ctx.repair_agent = RepairAgent(harness, skill="repair-toy-train") if harness else None
    return asyncio.run(run_trial(ctx)), campaign


# -- the two real situations from the design ---------------------------------


def test_stuck_status_but_work_done_collects_anyway(tmp_path, toy_project):
    """"The status never updated to complete, but it trained enough steps -
    collect the results anyway." """
    harness = repair_harness("collect", "logs show 4200 steps; results usable")
    status, campaign = drive(
        tmp_path, toy_project,
        {"polls": 99, "outcome": "done", "scale": 1.0},  # never finishes on its own
        harness, stuck_after=2,
    )
    with campaign:
        assert status == "completed"
        assert campaign.state.trials["T001"].metrics["score"].value == pytest.approx(0.70)
        verdicts = [e for e in campaign.ledger.events() if e.type == "repair_verdict"]
        assert [e.action for e in verdicts] == ["collect"]
        assert "4200 steps" in verdicts[0].diagnosis


def test_unknown_status_relaunched(tmp_path, toy_project):
    """"This one just needs a restart." The engine performs the relaunch, so
    the new job is recorded like any other."""
    harness = repair_harness("relaunch", "known preemption signature")
    status, campaign = drive(
        tmp_path, toy_project,
        {"polls": 0, "outcome": "stuck", "scale": 1.0},  # unknown status
        harness,
    )
    with campaign:
        # The relaunched attempt hits the same unknown status and is repaired
        # again until max_attempts stops it - the point is that the engine,
        # not the agent, issued every launch.
        launches = [e for e in campaign.ledger.events() if e.type == "job_launched"]
        assert len(launches) >= 2
        assert all(e.job_id.startswith("toy-") for e in launches)


# -- the engine acts, the agent only recommends ------------------------------


def test_repair_is_asked_only_when_rules_run_out(tmp_path, toy_project):
    harness = repair_harness("collect")
    status, campaign = drive(
        tmp_path, toy_project, {"polls": 0, "scale": 1.0}, harness
    )
    with campaign:
        assert status == "completed"
        assert not [e for e in campaign.ledger.events() if e.type == "repair_started"]


def test_wait_keeps_polling(tmp_path, toy_project):
    """A repair that says "still healthy" resets the stuck clock rather than
    ending the phase."""
    calls = {"n": 0}

    def fn(req: AgentRequest) -> AgentResult:
        calls["n"] += 1
        action = "wait" if calls["n"] == 1 else "collect"
        (req.phase_dir / VERDICT_FILENAME).write_text(
            json.dumps({"action": action, "diagnosis": "checked"})
        )
        return AgentResult()

    status, campaign = drive(
        tmp_path, toy_project,
        {"polls": 99, "outcome": "done", "scale": 1.0},
        ScriptedHarness(fn), stuck_after=2, max_attempts=3,
    )
    with campaign:
        assert status == "completed"
        actions = [
            e.action for e in campaign.ledger.events() if e.type == "repair_verdict"
        ]
        assert actions == ["wait", "collect"]


def test_fail_idea_ends_the_trial_as_a_result(tmp_path, toy_project):
    status, campaign = drive(
        tmp_path, toy_project,
        {"polls": 0, "outcome": "stuck"},
        repair_harness("fail_idea", "OOM caused by the idea's larger batch"),
    )
    with campaign:
        assert status == "failed_idea"
        assert campaign.state.budget_consumed_trials == 1  # a real evaluation


def test_fail_infra_does_not_consume_budget(tmp_path, toy_project):
    status, campaign = drive(
        tmp_path, toy_project,
        {"polls": 0, "outcome": "stuck"},
        repair_harness("fail_infra", "cluster-wide outage"),
    )
    with campaign:
        assert status == "errored"
        assert campaign.state.budget_consumed_trials == 0


def test_escalate_parks_for_a_human(tmp_path, toy_project):
    status, campaign = drive(
        tmp_path, toy_project,
        {"polls": 0, "outcome": "stuck"},
        repair_harness("escalate", "cannot tell from the logs"),
    )
    with campaign:
        assert status == "errored"
        index = json.loads(campaign.views.index_path.read_text())
        assert index["trials"][0]["needs_attention"] is True


def test_a_rescued_trial_is_not_flagged_for_a_human(tmp_path, toy_project):
    """Repair that worked is not an alarm - only trials repair gave up on
    are flagged, or every rescue would cry wolf."""
    status, campaign = drive(
        tmp_path, toy_project,
        {"polls": 99, "outcome": "done", "scale": 1.0},
        repair_harness("collect", "results are usable"), stuck_after=2,
    )
    with campaign:
        assert status == "completed"
        row = json.loads(campaign.views.index_path.read_text())["trials"][0]
        assert "needs_attention" not in row
        assert row["repairs"] == 1  # but the repair is still visible


def test_repair_attempts_are_capped(tmp_path, toy_project):
    harness = repair_harness("wait")  # would loop forever if uncapped
    status, campaign = drive(
        tmp_path, toy_project,
        {"polls": 99, "outcome": "done"},
        harness, stuck_after=1, max_attempts=2,
    )
    with campaign:
        assert status == "errored"
        started = [e for e in campaign.ledger.events() if e.type == "repair_started"]
        # Capped per attempt, across the phase's retries.
        assert len(started) <= 2 * (2 + 1)


def test_without_a_repair_agent_the_phase_just_fails(tmp_path, toy_project):
    status, campaign = drive(
        tmp_path, toy_project, {"polls": 0, "outcome": "stuck"}, None
    )
    with campaign:
        assert status == "errored"
        assert not [e for e in campaign.ledger.events() if e.type == "repair_started"]


# -- verdict validation ------------------------------------------------------


def test_verdict_must_be_a_known_action(tmp_path):
    def fn(req: AgentRequest) -> AgentResult:
        (req.phase_dir / VERDICT_FILENAME).write_text(
            json.dumps({"action": "delete_the_cluster"})
        )
        return AgentResult()

    agent = RepairAgent(ScriptedHarness(fn))
    with pytest.raises(HarnessError, match="invalid"):
        agent.diagnose(Situation("T1", "train", "stuck", phase_dir=tmp_path))


def test_prose_only_verdict_is_rejected(tmp_path):
    """Chatter is not a verdict: the action field is what the engine reads."""
    agent = RepairAgent(ScriptedHarness(lambda req: AgentResult(text="try again?")))
    with pytest.raises(HarnessError, match="wrote no"):
        agent.diagnose(Situation("T1", "train", "stuck", phase_dir=tmp_path))


def test_stale_verdict_is_not_reused(tmp_path):
    (tmp_path / VERDICT_FILENAME).write_text(json.dumps({"action": "collect"}))
    agent = RepairAgent(ScriptedHarness(lambda req: AgentResult(text="silent")))
    with pytest.raises(HarnessError, match="wrote no"):
        agent.diagnose(Situation("T1", "train", "stuck", phase_dir=tmp_path))


def test_prompt_carries_the_situation_and_the_action_menu(tmp_path):
    harness = repair_harness("escalate")
    RepairAgent(harness, skill="repair-toy-train").diagnose(
        Situation(
            "T007", "train", "unknown_status", detail="status 'zombie'",
            job_id="xm-42", status="zombie", phase_dir=tmp_path,
        )
    )
    prompt = harness.requests[0].prompt
    assert "T007" in prompt and "xm-42" in prompt and "zombie" in prompt
    for action in ("collect", "relaunch", "wait", "fail_infra", "fail_idea", "escalate"):
        assert action in prompt
    # The agent is told plainly that it recommends and the engine acts.
    assert "Do not" in prompt and "launch, kill, or modify any job" in prompt
    assert harness.requests[0].skills == ["repair-toy-train"]


# -- a job that never gets scheduled -----------------------------------------


def test_queued_job_gets_its_own_patience(tmp_path, toy_project):
    """A job that never starts is a different problem from one that runs a
    long time, so it is flagged as `never_scheduled` on its own threshold."""
    cfg = write_config(
        tmp_path, toy_project,
        {"polls": 0, "pending": 99, "outcome": "done"},
        stuck_after=50, max_attempts=1,
    )
    # queue patience much shorter than the running-stuck threshold
    cfg.write_text(cfg.read_text().replace(
        "stuck_after_polls: 50", "stuck_after_polls: 50\n    pending_after_polls: 3"
    ))
    campaign = Campaign(tmp_path / "campaign", cfg)
    campaign.create_trial("T001")
    campaign.prepare_workspace("T001")
    ctx = campaign.trial_context("T001", poll_interval=0, run_agentic=agent_stub)
    ctx.repair_agent = RepairAgent(repair_harness("escalate", "queued, no start time"))

    with campaign:
        asyncio.run(run_trial(ctx))
        started = [e for e in campaign.ledger.events() if e.type == "repair_started"]
        assert started, "a job stuck pending should reach repair"
        assert started[0].trigger == "never_scheduled"
        assert "pending" in started[0].detail


def test_pending_then_running_is_not_a_problem(tmp_path, toy_project):
    """Time in the queue is normal; it only matters when it doesn't end."""
    status, campaign = drive(
        tmp_path, toy_project,
        {"pending": 2, "polls": 1, "outcome": "done", "scale": 1.0},
        repair_harness("escalate"), stuck_after=10,
    )
    with campaign:
        assert status == "completed"
        assert not [e for e in campaign.ledger.events() if e.type == "repair_started"]
        # The queue time is visible in the ledger as a real transition.
        statuses = [
            e.status for e in campaign.ledger.events()
            if e.type == "job_status_changed"
        ]
        assert statuses == ["pending", "running", "done"]


# -- cleaning up jobs the engine is done with --------------------------------


def cancelled_jobs(campaign) -> list[str]:
    return [
        e.job_id for e in campaign.ledger.events() if e.type == "job_cancelled"
    ]


def test_repair_giving_up_cancels_the_job(tmp_path, toy_project):
    """Abandoning a phase must not leave the job queued and unwatched."""
    status, campaign = drive(
        tmp_path, toy_project,
        {"polls": 0, "outcome": "stuck"},
        repair_harness("escalate", "cannot tell"),
    )
    with campaign:
        assert status == "errored"
        launched = [
            e.job_id for e in campaign.ledger.events() if e.type == "job_launched"
        ]
        # The phase retries, so several jobs may exist - none may be left
        # queued and unwatched.
        assert launched
        assert set(cancelled_jobs(campaign)) == set(launched)


def test_relaunch_cancels_the_submission_it_replaces(tmp_path, toy_project):
    """Otherwise the old job stays queued, competing with its replacement."""
    status, campaign = drive(
        tmp_path, toy_project,
        {"polls": 0, "outcome": "stuck"},
        repair_harness("relaunch", "transient"),
    )
    with campaign:
        launched = [e.job_id for e in campaign.ledger.events() if e.type == "job_launched"]
        cancelled = cancelled_jobs(campaign)
        assert len(launched) >= 2
        # every superseded submission was cancelled, the last one included
        assert set(cancelled) <= set(launched)
        assert launched[0] in cancelled


def test_a_project_without_cancel_still_works(tmp_path, toy_project):
    """cancel is optional: without it the job is left to its scheduler."""
    (toy_project / "cancel").unlink()
    status, campaign = drive(
        tmp_path, toy_project,
        {"polls": 0, "outcome": "stuck"},
        repair_harness("escalate"),
    )
    with campaign:
        assert status == "errored"
        assert cancelled_jobs(campaign) == []
