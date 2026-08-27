"""Agent-backed ideation: what the ideator sees, and what it may return."""

import asyncio
import json
from pathlib import Path

import pytest

from autoresearch.agents import AgentRequest, AgentResult, HarnessError, ScriptedHarness
from autoresearch.campaign import Campaign
from autoresearch.ideator import IDEAS_FILENAME, AgentIdeator, research_digest
from autoresearch.loop import BASELINE_TRIAL, CampaignLoop
from autoresearch.contract import PhaseResult, write_result
from autoresearch.phases import finalize

CONFIG = """
name: t
goal: Make the toy score go up.
project_dir: {project}
key_metrics:
  score: {{from: train}}
ideation:
  backlog_target: 2
  skills: [toy-ideate]
budget:
  max_trials: 3
  active_trials: 1
workflow:
  implement: {{agentic: true, skills: [s], produces: [change.json]}}
  train: {{after: implement, uses: job, params: {{polls: 0, scale: 1.0}}}}
"""


def write_config(tmp_path: Path, project: Path) -> Path:
    path = tmp_path / "campaign.yaml"
    path.write_text(CONFIG.format(project=project))
    return path


def ideator_harness(ideas, malformed=None):
    """A harness that writes an ideas file, as a real ideator would."""

    def fn(req: AgentRequest) -> AgentResult:
        payload = malformed if malformed is not None else json.dumps({"ideas": ideas})
        (req.workspace / IDEAS_FILENAME).write_text(payload)
        return AgentResult(text="proposed some ideas")

    return ScriptedHarness(fn)


def agent_phase_runner(phase, cfg, workspace: Path, phase_dir: Path):
    idea_file = workspace / "idea.json"
    idea = json.loads(idea_file.read_text()) if idea_file.exists() else {}
    (workspace / "change.json").write_text(
        json.dumps({"name": idea.get("name", "base"), "delta": idea.get("delta", 0.0)})
    )
    write_result(phase_dir, PhaseResult(status="passed", notes=f"stub {phase}"))
    return finalize(phase_dir, cfg, verified=False, workspace=workspace)


# -- what the ideator sees ---------------------------------------------------


def test_digest_shows_trials_metrics_and_reports(tmp_path, toy_project):
    cfg = write_config(tmp_path, toy_project)
    with Campaign(tmp_path / "campaign", cfg) as c:
        c.create_trial("T001")
        from autoresearch import events as ev

        c.recorder.record(ev.PhaseStarted, trial="T001", phase="train")
        c.recorder.record(
            ev.PhaseCompleted, trial="T001", phase="train", status="passed",
            metrics={"score": 0.73}, verified=True,
        )
        c.recorder.record(ev.TrialFinished, trial="T001", status="completed")
        report = c.dir / "trials" / "T001" / "phases" / "analyze"
        report.mkdir(parents=True, exist_ok=True)
        (report / "report.md").write_text("Bigger LR helped early but plateaued.\n")

        digest = research_digest(c)
    assert "T001" in digest
    assert "score=0.73" in digest
    assert "plateaued" in digest  # the analysis reaches ideation


def test_unverified_metrics_are_labelled_for_the_ideator(tmp_path, toy_project):
    cfg = write_config(tmp_path, toy_project)
    with Campaign(tmp_path / "campaign", cfg) as c:
        from autoresearch import events as ev

        c.create_trial("T001")
        c.recorder.record(
            ev.MetricRecorded, trial="T001", metric="hunch", value=0.9, verified=False
        )
        assert "(unverified)" in research_digest(c)


def test_prompt_includes_goal_and_research(tmp_path, toy_project):
    cfg = write_config(tmp_path, toy_project)
    harness = ideator_harness([{"name": "a", "rationale": "r"}])
    with Campaign(tmp_path / "campaign", cfg) as c:
        AgentIdeator(harness)(c, wanted=1)
    prompt = harness.requests[0].prompt
    assert "Make the toy score go up." in prompt
    assert IDEAS_FILENAME in prompt
    assert "score (maximize)" in prompt
    assert harness.requests[0].skills == ["toy-ideate"]


# -- what the ideator may return ---------------------------------------------


def test_ideas_are_parsed_and_capped(tmp_path, toy_project):
    cfg = write_config(tmp_path, toy_project)
    harness = ideator_harness(
        [
            {"name": "a", "rationale": "r1", "delta": 0.2},
            {"name": "b", "rationale": "r2"},
            {"name": "c", "rationale": "r3"},
        ]
    )
    with Campaign(tmp_path / "campaign", cfg) as c:
        ideas = AgentIdeator(harness)(c, wanted=2)
    assert [i["name"] for i in ideas] == ["a", "b"]  # capped at what was asked
    assert ideas[0]["delta"] == 0.2  # project-specific fields survive


def test_parent_trial_is_carried(tmp_path, toy_project):
    cfg = write_config(tmp_path, toy_project)
    harness = ideator_harness([{"name": "a", "parent_trial": "T003"}])
    with Campaign(tmp_path / "campaign", cfg) as c:
        ideas = AgentIdeator(harness)(c, wanted=1)
    assert ideas[0]["parent_trial"] == "T003"


def test_missing_ideas_file_is_a_harness_error(tmp_path, toy_project):
    cfg = write_config(tmp_path, toy_project)
    harness = ScriptedHarness(lambda req: AgentResult(text="I had some thoughts"))
    with Campaign(tmp_path / "campaign", cfg) as c:
        with pytest.raises(HarnessError, match="wrote no"):
            AgentIdeator(harness)(c, wanted=1)


def test_malformed_ideas_rejected(tmp_path, toy_project):
    cfg = write_config(tmp_path, toy_project)
    harness = ideator_harness([], malformed='{"ideas": [{"rationale": "no name"}]}')
    with Campaign(tmp_path / "campaign", cfg) as c:
        with pytest.raises(HarnessError, match="invalid"):
            AgentIdeator(harness)(c, wanted=1)


def test_stale_ideas_file_is_not_reused(tmp_path, toy_project):
    """A harness that writes nothing must not silently re-serve last round's
    ideas."""
    cfg = write_config(tmp_path, toy_project)
    with Campaign(tmp_path / "campaign", cfg) as c:
        AgentIdeator(ideator_harness([{"name": "first"}]))(c, wanted=1)
        silent = ScriptedHarness(lambda req: AgentResult(text="nothing to add"))
        with pytest.raises(HarnessError, match="wrote no"):
            AgentIdeator(silent)(c, wanted=1)


# -- end to end --------------------------------------------------------------


def test_campaign_runs_on_agent_generated_ideas(tmp_path, toy_project):
    cfg = write_config(tmp_path, toy_project)
    harness = ideator_harness(
        [{"name": "lr-up", "delta": 0.3}, {"name": "lr-down", "delta": -0.1}]
    )
    with Campaign(tmp_path / "campaign", cfg) as c:
        loop = CampaignLoop(
            c, ideator=AgentIdeator(harness), run_agentic=agent_phase_runner,
            poll_interval=0,
        )
        result = asyncio.run(loop.run())
        assert result.status == "budget_reached"
        scores = {
            t.trial: t.metrics["score"].value
            for t in c.state.trials.values()
            if "score" in t.metrics
        }
        assert scores[BASELINE_TRIAL] == pytest.approx(0.50)
        # The agent's ideas actually changed outcomes.
        assert pytest.approx(0.80) in scores.values()
