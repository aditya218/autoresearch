"""Resolving a project's skills.

Skills are how a project teaches agents what it knows - how to propose ideas
for this task, how to implement them in this codebase, how its infrastructure
fails. They are plain markdown, and the engine resolves them itself rather
than relying on any one harness's discovery mechanism, so the same skill text
reaches a hosted SDK, a CLI, or a scripted stand-in unchanged.

Layout, beside the project's scripts:

    <project_dir>/skills/<name>/SKILL.md
    <project_dir>/skills/<name>.md      (also accepted)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class Skill:
    name: str
    text: str
    path: Path


class SkillNotFound(Exception):
    """A phase named a skill the project does not provide."""


def skills_dir(project_dir: str | Path) -> Path:
    return Path(project_dir) / "skills"


def find(project_dir: str | Path, name: str) -> Skill:
    root = skills_dir(project_dir)
    for candidate in (root / name / "SKILL.md", root / f"{name}.md"):
        if candidate.exists():
            return Skill(name=name, text=candidate.read_text(), path=candidate)
    raise SkillNotFound(
        f"skill {name!r} not found under {root} "
        f"(expected {name}/SKILL.md or {name}.md)"
    )


def resolve(project_dir: str | Path, names: list[str]) -> list[Skill]:
    """Load every named skill, or raise naming the first one missing."""
    return [find(project_dir, name) for name in names]


def available(project_dir: str | Path) -> list[str]:
    root = skills_dir(project_dir)
    if not root.exists():
        return []
    names = {p.parent.name for p in root.glob("*/SKILL.md")}
    names |= {p.stem for p in root.glob("*.md")}
    return sorted(names)


def as_prompt_section(skills: list[Skill]) -> str:
    """Render skills for inclusion in a phase prompt."""
    if not skills:
        return ""
    parts = ["# Skills", "", "Follow these. They are how this project works."]
    for skill in skills:
        parts += ["", f"## Skill: {skill.name}", "", skill.text.strip()]
    return "\n".join(parts)
