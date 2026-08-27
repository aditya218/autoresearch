"""Engine-mediated job tools: an agent can launch jobs without ever holding
an unrecorded job_id."""

import json

import pytest

from autoresearch import events as ev
from autoresearch.campaign import Campaign
from autoresearch.tools import JobTools

CONFIG = """
name: t
goal: g
project_dir: {project}
workflow:
  explore: {{agentic: true, skills: [s]}}
"""


def make(tmp_path, toy_project) -> tuple[Campaign, JobTools]:
    cfg = tmp_path / "campaign.yaml"
    cfg.write_text(CONFIG.format(project=toy_project))
    campaign = Campaign(tmp_path / "campaign", cfg)
    campaign.create_trial("T001")
    ws = campaign.prepare_workspace("T001")
    (ws / "change.json").write_text(json.dumps({"name": "idea", "delta": 0.2}))
    campaign.recorder.record(ev.PhaseStarted, trial="T001", phase="explore", agentic=True)
    tools = JobTools(
        project=campaign.project, recorder=campaign.recorder, trial_id="T001",
        phase="explore", workspace=ws,
        phase_dir=campaign.dir / "trials" / "T001" / "phases" / "explore",
        campaign="t",
    )
    return campaign, tools


def test_launch_is_recorded_before_the_agent_sees_the_id(tmp_path, toy_project):
    campaign, tools = make(tmp_path, toy_project)
    with campaign:
        job_id = tools.launch_job({"polls": 0, "scale": 1.0})
        launched = [e for e in campaign.ledger.events() if e.type == "job_launched"]
        assert [e.job_id for e in launched] == [job_id]
        # And the ledger's live state can reattach to it after a restart.
        assert campaign.state.launched_job_ids == [job_id]


def test_agent_can_drive_a_job_to_results(tmp_path, toy_project):
    campaign, tools = make(tmp_path, toy_project)
    with campaign:
        job_id = tools.launch_job({"polls": 1, "scale": 1.0})
        assert tools.poll_job(job_id) == "running"
        assert tools.poll_job(job_id) == "done"
        out = tools.collect_job(job_id)
        result = json.loads((tools.phase_dir / "result.json").read_text())
        assert result["metrics"]["score"] == pytest.approx(0.70)
        assert str(tools.phase_dir) == out


def test_multiple_jobs_in_one_phase_are_each_recorded(tmp_path, toy_project):
    """Freeform mode: one agentic phase may launch several jobs."""
    campaign, tools = make(tmp_path, toy_project)
    with campaign:
        first = tools.launch_job({"polls": 0}, suffix="/a")
        second = tools.launch_job({"polls": 0}, suffix="/b")
        launched = [e for e in campaign.ledger.events() if e.type == "job_launched"]
        assert {e.job_id for e in launched} == {first, second}
        assert {e.tag for e in launched} == {"t/T001/explore/a", "t/T001/explore/b"}


def test_poll_transitions_are_recorded(tmp_path, toy_project):
    campaign, tools = make(tmp_path, toy_project)
    with campaign:
        job_id = tools.launch_job({"polls": 0})
        tools.poll_job(job_id)
        statuses = [
            e.status for e in campaign.ledger.events() if e.type == "job_status_changed"
        ]
        assert statuses == ["done"]


def test_tools_dict_exposes_the_three_calls(tmp_path, toy_project):
    campaign, tools = make(tmp_path, toy_project)
    with campaign:
        assert set(tools.as_dict()) == {"launch_job", "poll_job", "collect_job"}
