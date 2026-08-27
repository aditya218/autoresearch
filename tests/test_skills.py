"""Project-supplied skills: resolution, and how they reach an agent."""

import json
from pathlib import Path

import pytest

from autoresearch import skills
from autoresearch.agents import AgentRequest, AgentResult, ScriptedHarness
from autoresearch.agentic import make_agentic_runner
from autoresearch.campaign import Campaign
from autoresearch.contract import PhaseResult, write_result
from autoresearch.phases import PhaseFailure, finalize

REPO = Path(__file__).resolve().parent.parent
EXAMPLE = REPO / "examples" / "slurm_mle"


def make_skill(root: Path, name: str, text: str, flat: bool = False) -> Path:
    if flat:
        path = root / "skills" / f"{name}.md"
    else:
        path = root / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# -- resolution --------------------------------------------------------------


def test_finds_skill_in_either_layout(tmp_path):
    make_skill(tmp_path, "nested", "nested skill body")
    make_skill(tmp_path, "flat", "flat skill body", flat=True)
    assert skills.find(tmp_path, "nested").text == "nested skill body"
    assert skills.find(tmp_path, "flat").text == "flat skill body"


def test_missing_skill_names_itself(tmp_path):
    with pytest.raises(skills.SkillNotFound, match="ghost"):
        skills.find(tmp_path, "ghost")


def test_available_lists_both_layouts(tmp_path):
    make_skill(tmp_path, "a", "x")
    make_skill(tmp_path, "b", "y", flat=True)
    assert skills.available(tmp_path) == ["a", "b"]


def test_prompt_section_carries_every_skill(tmp_path):
    make_skill(tmp_path, "one", "first body")
    make_skill(tmp_path, "two", "second body")
    section = skills.as_prompt_section(skills.resolve(tmp_path, ["one", "two"]))
    assert "first body" in section and "second body" in section
    assert "## Skill: one" in section


# -- the example's own skills ------------------------------------------------


def test_example_provides_every_skill_its_config_names():
    """The campaign config and the skills directory must agree, or a real
    run fails at the first agentic phase."""
    from autoresearch.config import load_config

    cfg = load_config(EXAMPLE / "campaign.yaml")
    named = set(cfg.ideation.skills)
    for phase in cfg.workflow.values():
        named |= set(phase.skills)
        if phase.repair:
            named.add(phase.repair.skill)

    missing = named - set(skills.available(EXAMPLE))
    assert not missing, f"campaign.yaml names skills that don't exist: {missing}"


# -- skills reaching the agent -----------------------------------------------


CONFIG = """
name: t
goal: g
project_dir: {project}
workflow:
  implement: {{agentic: true, skills: [house-style]}}
"""


def test_skill_text_is_inlined_into_the_phase_prompt(tmp_path, toy_project):
    make_skill(toy_project, "house-style", "ALWAYS prefer stdlib.")
    cfg_path = tmp_path / "campaign.yaml"
    cfg_path.write_text(CONFIG.format(project=toy_project))

    seen = {}

    def fn(req: AgentRequest) -> AgentResult:
        seen["prompt"] = req.prompt
        write_result(req.phase_dir, PhaseResult(status="passed"))
        return AgentResult()

    with Campaign(tmp_path / "campaign", cfg_path) as campaign:
        campaign.create_trial("T001")
        ws = campaign.prepare_workspace("T001")
        runner = make_agentic_runner(
            ScriptedHarness(fn), campaign.config, campaign.state, "T001",
            project_dir=toy_project,
        )
        runner(
            "implement", campaign.config.workflow["implement"], ws,
            tmp_path / "phase",
        )

    assert "ALWAYS prefer stdlib." in seen["prompt"]


def test_a_missing_skill_fails_the_phase_clearly(tmp_path, toy_project):
    """Better to fail at the phase than to run an agent without the
    instructions the project meant it to have."""
    cfg_path = tmp_path / "campaign.yaml"
    cfg_path.write_text(CONFIG.format(project=toy_project))

    with Campaign(tmp_path / "campaign", cfg_path) as campaign:
        campaign.create_trial("T001")
        ws = campaign.prepare_workspace("T001")
        runner = make_agentic_runner(
            ScriptedHarness(lambda req: AgentResult()),
            campaign.config, campaign.state, "T001", project_dir=toy_project,
        )
        with pytest.raises(PhaseFailure, match="house-style"):
            runner(
                "implement", campaign.config.workflow["implement"], ws,
                tmp_path / "phase",
            )
