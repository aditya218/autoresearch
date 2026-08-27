"""Agent-backed ideation.

The ideator reads the research so far from the ledger's views - never the raw
event log - and proposes ideas that build on it. It is the only component
that sees everything, so the views are effectively its API (§8).

Its output goes through a schema like any other agent output: an idea that
doesn't parse is dropped with a warning rather than poisoning the backlog.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from autoresearch.agents import AgentHarness, AgentRequest, HarnessError

IDEAS_FILENAME = "ideas.json"


class Idea(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    rationale: str = ""
    #: trial this idea builds on, if any - how the tree of ideas grows
    parent_trial: str | None = None


class IdeaBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ideas: list[Idea] = Field(default_factory=list)


PROMPT = """\
# Campaign: {name}
Goal: {goal}

# Research so far
{research}

# Your task
Propose {wanted} new experiment idea(s) that build on what the research so far
shows. Prefer ideas that test a distinct hypothesis rather than variations of
one that already failed. Where an idea extends a specific trial, name it as
`parent_trial` so the work branches from that trial's code.

{metrics_note}

Write {ideas_file} into the working directory:

    {{"ideas": [{{"name": "short-slug", "rationale": "why this is worth a trial",
                 "parent_trial": "T003" | null}}]}}

Include any extra fields the project's implement phase needs. Propose only
ideas you can justify from the evidence above; do not invent results.
"""


def research_digest(campaign, max_trials: int = 40) -> str:
    """What the ideator gets to see: the campaign index, plus the analysis
    each finished trial produced."""
    index_path = campaign.views.index_path
    if not index_path.exists():
        return "(no trials yet)"
    index = json.loads(index_path.read_text())
    rows = index.get("trials", [])[-max_trials:]
    if not rows:
        return "(no trials yet)"

    lines = []
    for row in rows:
        metrics = ", ".join(
            f"{name}={m['value']}" + ("" if m["verified"] else " (unverified)")
            for name, m in row.get("metrics", {}).items()
        )
        parent = f" (from {row['parent_trial']})" if row.get("parent_trial") else ""
        lines.append(f"- {row['trial']}{parent}: {row['status']}  {metrics}")
        report = (
            campaign.dir / "trials" / row["trial"] / "phases" / "analyze" / "report.md"
        )
        if report.exists():
            excerpt = report.read_text().strip().splitlines()
            lines += [f"    {line}" for line in excerpt[:6]]
    return "\n".join(lines)


class AgentIdeator:
    """An ideator backed by an agent harness."""

    def __init__(
        self,
        harness: AgentHarness,
        work_dir: Path | None = None,
        project_dir: Path | None = None,
    ):
        self.harness = harness
        self.work_dir = work_dir
        self.project_dir = project_dir
        self.dropped: list[str] = []

    def __call__(self, campaign, wanted: int) -> list[dict]:
        cfg = campaign.config
        work_dir = Path(self.work_dir or campaign.dir / "ideation")
        work_dir.mkdir(parents=True, exist_ok=True)
        out_file = work_dir / IDEAS_FILENAME
        if out_file.exists():
            out_file.unlink()

        metrics_note = (
            "Key metrics: "
            + ", ".join(
                f"{m} ({c.goal})" for m, c in cfg.key_metrics.items()
            )
            if cfg.key_metrics
            else ""
        )
        prompt = PROMPT.format(
            name=cfg.name,
            goal=cfg.goal,
            research=research_digest(campaign),
            wanted=wanted,
            metrics_note=metrics_note,
            ideas_file=IDEAS_FILENAME,
        )
        if cfg.ideation.prompt:
            prompt += "\n" + cfg.ideation.prompt

        # Inline the project's ideation skills, the same way phases get theirs.
        if cfg.ideation.skills and self.project_dir is not None:
            from autoresearch.skills import SkillNotFound, as_prompt_section, resolve

            try:
                skills = resolve(self.project_dir, cfg.ideation.skills)
            except SkillNotFound as exc:
                raise HarnessError(str(exc)) from exc
            prompt = as_prompt_section(skills) + "\n\n" + prompt

        result = self.harness.invoke(
            AgentRequest(
                prompt=prompt,
                skills=cfg.ideation.skills,
                workspace=work_dir,
                phase_dir=work_dir,
            )
        )
        if not result.ok:
            raise HarnessError(f"ideation harness failed: {result.detail}")

        return self._read_ideas(out_file, wanted)

    def _read_ideas(self, out_file: Path, wanted: int) -> list[dict]:
        if not out_file.exists():
            raise HarnessError(f"ideator wrote no {IDEAS_FILENAME}")
        try:
            batch = IdeaBatch.model_validate_json(out_file.read_text())
        except ValidationError as exc:
            raise HarnessError(f"invalid {IDEAS_FILENAME}: {exc}") from exc

        ideas: list[dict] = []
        for idea in batch.ideas[:wanted]:
            payload = idea.model_dump()
            if not payload["name"].strip():
                self.dropped.append("idea with an empty name")
                continue
            ideas.append(payload)
        return ideas
