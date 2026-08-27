"""Running agentic phases and ideation through a harness.

The engine composes the context an agent needs, invokes the harness, then
reads the phase's outcome from `result.json` exactly as it would for a
deterministic phase. The harness's own chatter is advisory: it is recorded as
notes, never acted on (§7.1).
"""

from __future__ import annotations

import json
from pathlib import Path

from autoresearch.agents import AgentHarness, AgentRequest, HarnessError
from autoresearch.config import CampaignConfig, PhaseConfig
from autoresearch.contract import RESULT_FILENAME
from autoresearch.phases import PhaseFailure, PhaseOutcome, finalize

CONTRACT_NOTE = f"""
When you are done, write {RESULT_FILENAME} into the phase directory:

    {{"status": "passed" | "failed", "metrics": {{"name": number}},
      "notes": "what you did and why", "artifacts": ["relative paths"]}}

Rules:
  - `status` must be exactly "passed" or "failed" - the engine reads this
    field to decide whether the trial continues. Do not describe the outcome
    in prose instead.
  - Report only numbers you actually measured. Metrics the campaign has bound
    to another phase are ignored if you report them.
  - Write only inside the workspace and the phase directory. Earlier phases'
    outputs and the project's scripts are read-only.
""".strip()


def compose_prompt(
    config: CampaignConfig,
    phase: str,
    cfg: PhaseConfig,
    trial_id: str,
    workspace: Path,
    phase_dir: Path,
    idea: dict | None = None,
    prior: dict[str, dict] | None = None,
    skill_text: str = "",
) -> str:
    """Build the phase prompt: campaign goal, the project's skills, the idea,
    what earlier phases found, where to work, and the output contract."""
    lines = [
        f"# Campaign: {config.name}",
        f"Goal: {config.goal}",
        "",
        f"# Phase: {phase} (trial {trial_id})",
    ]
    if cfg.prompt:
        lines += ["", cfg.prompt]
    if skill_text:
        lines += ["", skill_text]
    elif cfg.skills:
        lines += ["", "Use these skills: " + ", ".join(cfg.skills)]
    if idea:
        lines += ["", "# The idea to evaluate", json.dumps(idea, indent=2)]
    if prior:
        lines += ["", "# What earlier phases found"]
        for name, summary in prior.items():
            lines.append(f"- {name}: {json.dumps(summary)}")
    if config.key_metrics:
        bindings = ", ".join(
            f"{m} (from {c.from_phase}, {c.goal})"
            for m, c in config.key_metrics.items()
        )
        lines += ["", f"# Key metrics for this campaign: {bindings}"]
    lines += [
        "",
        "# Where to work",
        f"- workspace (edit code here): {workspace}",
        f"- phase directory (write outputs here): {phase_dir}",
        "",
        CONTRACT_NOTE,
    ]
    return "\n".join(lines)


def prior_summaries(trial) -> dict[str, dict]:
    """A compact digest of completed phases for the prompt - status, metrics,
    and the notes each phase left behind."""
    summaries: dict[str, dict] = {}
    for name, phase in trial.phases.items():
        if phase.status not in ("passed", "failed"):
            continue
        summary: dict = {"status": phase.status, "notes": phase.notes}
        metrics = {k: v.value for k, v in trial.metrics.items() if v.phase == name}
        if metrics:
            summary["metrics"] = metrics
        summaries[name] = summary
    return summaries


def make_agentic_runner(
    harness: AgentHarness,
    config: CampaignConfig,
    state,
    trial_id: str,
    idea: dict | None = None,
    tools: dict | None = None,
    read_only: list[Path] | None = None,
    project_dir: Path | None = None,
):
    """Build the `run_agentic` callable `run_trial` expects.

    When `project_dir` is given, the phase's skills are resolved from it and
    inlined into the prompt, so they reach any harness identically.
    """

    def run(
        phase: str, cfg: PhaseConfig, workspace: Path, phase_dir: Path
    ) -> PhaseOutcome:
        trial = state.trials[trial_id]
        skill_text = ""
        if project_dir is not None and cfg.skills:
            from autoresearch.skills import SkillNotFound, as_prompt_section, resolve

            try:
                skill_text = as_prompt_section(resolve(project_dir, cfg.skills))
            except SkillNotFound as exc:
                raise PhaseFailure(f"{phase}: {exc}") from exc

        prompt = compose_prompt(
            config, phase, cfg, trial_id, workspace, phase_dir,
            idea=idea, prior=prior_summaries(trial), skill_text=skill_text,
        )
        request = AgentRequest(
            prompt=prompt,
            skills=cfg.skills,
            workspace=workspace,
            phase_dir=phase_dir,
            tools=tools or {},
            timeout_s=cfg.timeout_s,
            read_only=read_only or [],
        )
        try:
            result = harness.invoke(request)
        except HarnessError as exc:
            raise PhaseFailure(f"{phase}: {exc}") from exc

        if not result.ok:
            raise PhaseFailure(f"{phase}: agent harness failed: {result.detail}")

        # The agent's own words never decide anything: the outcome comes from
        # the contract file, and a missing or malformed one is a retryable
        # infra error.
        outcome = finalize(phase_dir, cfg, verified=False, workspace=workspace)
        if result.text and not outcome.notes:
            outcome.notes = result.text.strip()[-500:]
        return outcome

    return run
