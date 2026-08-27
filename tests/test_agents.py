"""Harness adapters, agentic phase execution, and prompt composition."""

import asyncio
import json
from pathlib import Path

import pytest

from autoresearch.agentic import compose_prompt, make_agentic_runner
from autoresearch.agents import (
    AgentRequest,
    AgentResult,
    CommandHarness,
    HarnessError,
    ScriptedHarness,
)
from autoresearch.campaign import Campaign
from autoresearch.config import PhaseConfig
from autoresearch.contract import PhaseResult, write_result
from autoresearch.engine import run_trial
from autoresearch.phases import PhaseFailure

CONFIG = """
name: t
goal: Make the toy score go up.
project_dir: {project}
key_metrics:
  score: {{from: train}}
workflow:
  implement: {{agentic: true, skills: [implement-idea], produces: [change.json]}}
  train: {{after: implement, uses: job, params: {{polls: 0, scale: 1.0}}}}
  analyze: {{after: train, agentic: true, skills: [analyze-results]}}
"""


def write_config(tmp_path: Path, project: Path) -> Path:
    path = tmp_path / "campaign.yaml"
    path.write_text(CONFIG.format(project=project))
    return path


def good_agent(delta: float = 0.2):
    """A harness that does the work and honours the contract."""

    def fn(req: AgentRequest) -> AgentResult:
        if "implement" in req.prompt:
            (req.workspace / "change.json").write_text(
                json.dumps({"name": "idea", "delta": delta})
            )
        write_result(req.phase_dir, PhaseResult(status="passed", notes="did the thing"))
        return AgentResult(text="all done")

    return ScriptedHarness(fn)


# -- prompt composition ------------------------------------------------------


def test_prompt_carries_goal_idea_contract_and_prior_findings(tmp_path, toy_project):
    from autoresearch.config import load_config

    cfg = load_config(write_config(tmp_path, toy_project))
    prompt = compose_prompt(
        cfg, "analyze", cfg.workflow["analyze"], "T007",
        tmp_path / "ws", tmp_path / "phase",
        idea={"name": "bigger-lr", "delta": 0.2},
        prior={"train": {"status": "passed", "metrics": {"score": 0.7}}},
    )
    assert "Make the toy score go up." in prompt
    assert "bigger-lr" in prompt
    assert "analyze-results" in prompt  # skills named
    assert '"score": 0.7' in prompt or "score" in prompt  # prior findings
    assert "result.json" in prompt  # the contract
    assert "score (from train" in prompt  # metric binding is stated
    assert str(tmp_path / "ws") in prompt


# -- agentic phases through a harness ----------------------------------------


def run_campaign_trial(tmp_path, toy_project, harness, idea=None) -> tuple[str, Campaign]:
    cfg = write_config(tmp_path, toy_project)
    campaign = Campaign(tmp_path / "campaign", cfg)
    campaign.create_trial("T001")
    campaign.prepare_workspace("T001")
    runner = make_agentic_runner(
        harness, campaign.config, campaign.state, "T001", idea=idea
    )
    ctx = campaign.trial_context("T001", poll_interval=0, run_agentic=runner)
    return asyncio.run(run_trial(ctx)), campaign


def test_agentic_phases_run_through_the_harness(tmp_path, toy_project):
    harness = good_agent(delta=0.2)
    status, campaign = run_campaign_trial(tmp_path, toy_project, harness)
    with campaign:
        assert status == "completed"
        assert campaign.state.trials["T001"].metrics["score"].value == pytest.approx(0.70)
        # Both agentic phases were invoked, with their configured skills.
        skills = [tuple(r.skills) for r in harness.requests]
        assert skills == [("implement-idea",), ("analyze-results",)]


def test_idea_reaches_the_agent(tmp_path, toy_project):
    harness = good_agent()
    _, campaign = run_campaign_trial(
        tmp_path, toy_project, harness, idea={"name": "bigger-lr", "delta": 0.2}
    )
    with campaign:
        assert "bigger-lr" in harness.requests[0].prompt


def test_agent_reporting_no_result_is_a_retryable_failure(tmp_path, toy_project):
    """Chatter is not a result: an agent that only talks fails the phase."""
    harness = ScriptedHarness(lambda req: AgentResult(text="I think it went well!"))
    status, campaign = run_campaign_trial(tmp_path, toy_project, harness)
    with campaign:
        assert status == "errored"
        errors = [e for e in campaign.ledger.events() if e.type == "phase_errored"]
        assert errors and "result.json" in errors[0].error


def test_harness_failure_is_a_phase_failure(tmp_path, toy_project):
    def boom(req):
        raise HarnessError("harness exploded")

    status, campaign = run_campaign_trial(tmp_path, toy_project, ScriptedHarness(boom))
    with campaign:
        assert status == "errored"


def test_agent_notes_are_carried_but_inert(tmp_path, toy_project):
    def fn(req: AgentRequest) -> AgentResult:
        if "implement" in req.prompt:
            (req.workspace / "change.json").write_text(json.dumps({"delta": 0.2}))
        write_result(req.phase_dir, PhaseResult(status="passed", notes="my notes"))
        return AgentResult(text="chatter that must not decide anything")

    status, campaign = run_campaign_trial(tmp_path, toy_project, ScriptedHarness(fn))
    with campaign:
        assert status == "completed"
        assert campaign.state.trials["T001"].phases["implement"].notes == "my notes"


# -- command harness ---------------------------------------------------------


def make_fake_cli(tmp_path: Path, body: str) -> Path:
    """A stand-in for any harness CLI: reads the prompt on stdin, works in
    cwd, exits 0."""
    path = tmp_path / "fake-harness"
    path.write_text("#!/usr/bin/env python3\n" + body)
    path.chmod(0o755)
    return path


def test_command_harness_runs_a_cli_in_the_workspace(tmp_path, monkeypatch):
    cli = make_fake_cli(
        tmp_path,
        "import sys, pathlib\n"
        "prompt = sys.stdin.read()\n"
        "pathlib.Path('seen_prompt.txt').write_text(prompt)\n"
        "print('done')\n",
    )
    monkeypatch.setenv("PATH", f"{tmp_path}:{__import__('os').environ['PATH']}")
    ws = tmp_path / "ws"
    ws.mkdir()

    harness = CommandHarness(command=[cli.name])
    result = harness.invoke(AgentRequest(prompt="hello agent", workspace=ws))
    assert result.ok is True
    assert result.text.strip() == "done"
    assert (ws / "seen_prompt.txt").read_text() == "hello agent"


def test_command_harness_reports_failure(tmp_path, monkeypatch):
    cli = make_fake_cli(
        tmp_path, "import sys\nsys.stderr.write('bad things\\n')\nsys.exit(2)\n"
    )
    monkeypatch.setenv("PATH", f"{tmp_path}:{__import__('os').environ['PATH']}")
    ws = tmp_path / "ws"
    ws.mkdir()
    result = CommandHarness(command=[cli.name]).invoke(
        AgentRequest(prompt="p", workspace=ws)
    )
    assert result.ok is False
    assert "bad things" in result.detail


def test_command_harness_passes_skill_flags(tmp_path, monkeypatch):
    cli = make_fake_cli(
        tmp_path,
        "import sys, pathlib\n"
        "sys.stdin.read()\n"
        "pathlib.Path('argv.txt').write_text(' '.join(sys.argv[1:]))\n",
    )
    monkeypatch.setenv("PATH", f"{tmp_path}:{__import__('os').environ['PATH']}")
    ws = tmp_path / "ws"
    ws.mkdir()
    CommandHarness(command=[cli.name], skill_arg="--skill").invoke(
        AgentRequest(prompt="p", skills=["a", "b"], workspace=ws)
    )
    assert (ws / "argv.txt").read_text() == "--skill a --skill b"


def test_missing_harness_binary_raises(tmp_path):
    with pytest.raises(HarnessError, match="not found on PATH"):
        CommandHarness(command=["definitely-not-a-real-harness-xyz"]).invoke(
            AgentRequest(prompt="p", workspace=tmp_path)
        )
