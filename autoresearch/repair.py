"""The repair agent: best-effort recovery when the engine has no rule.

Remote jobs fail in odd ways no rule set can anticipate - a status that never
updates although the logs show the work finished, a job that experience says
just needs a restart. Rules cover the known cases (§9); this covers the long
tail, by asking an agent to look and recommend (§9.1).

The invariant that shapes everything here: **the agent recommends, the engine
acts**. A repair can never launch or kill a job itself, so it can never create
a job the ledger does not know about.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from autoresearch.agents import AgentHarness, AgentRequest, HarnessError

VERDICT_FILENAME = "repair.json"


class Action(StrEnum):
    """The only actions a repair may recommend."""

    COLLECT = "collect"          # results are usable despite the status
    RELAUNCH = "relaunch"        # transient failure; run it again
    WAIT = "wait"                # still healthy, keep polling
    FAIL_INFRA = "fail_infra"    # infrastructure's fault, not the idea's
    FAIL_IDEA = "fail_idea"      # the idea genuinely failed
    ESCALATE = "escalate"        # no confident call; ask a human


class Verdict(BaseModel):
    """What the agent returns. `diagnosis` is prose and inert; `action` is the
    validated field the engine acts on (§7.1)."""

    model_config = ConfigDict(extra="forbid")

    action: Action
    diagnosis: str = ""
    params: dict = Field(default_factory=dict)


@dataclass
class Situation:
    """What the engine could not resolve, and everything known about it."""

    trial_id: str
    phase: str
    trigger: str            # ambiguous_launch | unknown_status | stuck |
                            # never_scheduled | collect_failed
    detail: str = ""
    job_id: str | None = None
    status: str | None = None
    phase_dir: Path = Path(".")
    workspace: Path = Path(".")
    history: list[dict] = field(default_factory=list)


PROMPT = """\
# Repair request

A job phase hit a situation the engine has no rule for. Investigate and
recommend exactly one action.

  trial:   {trial}
  phase:   {phase}
  job id:  {job_id}
  trigger: {trigger}
  status:  {status}
  detail:  {detail}

# What the ledger records about this job
{history}

# Where to look
- phase directory (job outputs and logs, read-only): {phase_dir}
- workspace (the trial's code, read-only): {workspace}

You may read logs and re-run the project's `poll` script to look again. Do not
launch, kill, or modify any job yourself - the engine performs whichever
action you recommend, so that every change is recorded.

Write {verdict_file} into the phase directory:

    {{"action": "collect" | "relaunch" | "wait" | "fail_infra" | "fail_idea"
                | "escalate",
      "diagnosis": "what you found, citing what you looked at",
      "params": {{}}}}

  collect     - the job did enough work; its results are usable as they are
  relaunch    - a transient failure; running it again should work
  wait        - the job is healthy, keep polling
  fail_infra  - infrastructure's fault; the idea was never really tested
  fail_idea   - the idea itself failed
  escalate    - you cannot tell; a human should look

If the trigger is `never_scheduled`, the job has been queued this whole time
and has consumed nothing: it has not started, so there are no results and no
partial work to salvage. Ask why it cannot be placed - usually the resources
it requested are unavailable - and decide whether waiting is still reasonable
or whether a human should resubmit it smaller.

Recommend `escalate` rather than guessing. Base the diagnosis on what you
actually read; do not invent results.
"""


def compose_prompt(situation: Situation) -> str:
    return PROMPT.format(
        trial=situation.trial_id,
        phase=situation.phase,
        job_id=situation.job_id or "(none - the launch was ambiguous)",
        trigger=situation.trigger,
        status=situation.status or "(unknown)",
        detail=situation.detail or "(none)",
        history=json.dumps(situation.history, indent=2) if situation.history else "(none)",
        phase_dir=situation.phase_dir,
        workspace=situation.workspace,
        verdict_file=VERDICT_FILENAME,
    )


def job_history(state, trial_id: str, phase: str) -> list[dict]:
    """What the ledger knows about this phase's job: launches and every
    status transition, in order."""
    trial = state.trials.get(trial_id)
    if trial is None:
        return []
    phase_state = trial.phases.get(phase)
    if phase_state is None:
        return []
    return [
        {
            "job_id": phase_state.job_id,
            "status": phase_state.job_status,
            "phase_status": phase_state.status,
            "attempt": phase_state.attempt,
        }
    ]


class RepairAgent:
    """Asks a harness to diagnose one situation and return a verdict."""

    def __init__(self, harness: AgentHarness, skill: str | None = None):
        self.harness = harness
        self.skill = skill

    def diagnose(self, situation: Situation) -> Verdict:
        verdict_path = situation.phase_dir / VERDICT_FILENAME
        if verdict_path.exists():
            verdict_path.unlink()  # never re-serve a previous verdict

        result = self.harness.invoke(
            AgentRequest(
                prompt=compose_prompt(situation),
                skills=[self.skill] if self.skill else [],
                workspace=situation.phase_dir,
                phase_dir=situation.phase_dir,
            )
        )
        if not result.ok:
            raise HarnessError(f"repair harness failed: {result.detail}")
        if not verdict_path.exists():
            raise HarnessError(f"repair agent wrote no {VERDICT_FILENAME}")
        try:
            return Verdict.model_validate_json(verdict_path.read_text())
        except ValidationError as exc:
            raise HarnessError(f"invalid {VERDICT_FILENAME}: {exc}") from exc


def repair_skill_for(cfg) -> str | None:
    """The repair skill a phase configured - where a project writes down its
    infra folklore, per job type (§9.1)."""
    return cfg.repair.skill if cfg.repair else None


def max_attempts_for(cfg) -> int:
    return cfg.repair.max_attempts if cfg.repair else 0
