"""Resolving `uses:` to a phase implementation.

A phase's `uses:` names how it runs. Four forms, all resolving to the same
thing - a directory of scripts and whether it is a job phase:

    uses: local            the project's own `run` script
    uses: job              the project's own launch/poll/collect[/find/cancel]
    uses: slurm_job        a shared implementation shipped beside the engine
    uses: ./phases/my_sim  a custom directory in the project

Shared phases are not special (§7.2): they are directories of exactly the
same scripts, living beside the engine rather than in it, so the engine core
never learns what Slurm is. An unknown `uses:` is an error - it used to fall
through to the project's `run` script, which meant a typo ran the wrong
thing silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

SHARED_ROOT = Path(__file__).resolve().parent / "shared_phases"

JOB_SCRIPTS = ("launch", "poll", "collect")
LOCAL_SCRIPTS = ("run",)


class PhaseImplError(Exception):
    """`uses:` does not name anything the engine can run."""


@dataclass(frozen=True)
class PhaseImpl:
    """Where a phase's scripts live, and how the engine drives them."""

    name: str
    kind: str  # "job" | "local"
    scripts_dir: Path

    @property
    def is_job(self) -> bool:
        return self.kind == "job"


def shared_names() -> list[str]:
    if not SHARED_ROOT.exists():
        return []
    return sorted(p.name for p in SHARED_ROOT.iterdir() if p.is_dir())


def _kind_of(scripts_dir: Path) -> str:
    if all((scripts_dir / s).exists() for s in JOB_SCRIPTS):
        return "job"
    if (scripts_dir / "run").exists():
        return "local"
    raise PhaseImplError(
        f"{scripts_dir} provides neither a job phase "
        f"({', '.join(JOB_SCRIPTS)}) nor a local one (run)"
    )


def resolve(uses: str, project_dir: str | Path) -> PhaseImpl:
    project_dir = Path(project_dir)

    if uses == "local":
        return PhaseImpl("local", "local", project_dir)
    if uses == "job":
        return PhaseImpl("job", "job", project_dir)

    if uses.startswith(("./", "../", "/")):
        path = Path(uses)
        scripts_dir = path if path.is_absolute() else (project_dir / uses).resolve()
        if not scripts_dir.exists():
            raise PhaseImplError(f"custom phase directory not found: {scripts_dir}")
        return PhaseImpl(uses, _kind_of(scripts_dir), scripts_dir)

    shared = SHARED_ROOT / uses
    if shared.exists():
        return PhaseImpl(uses, _kind_of(shared), shared)

    raise PhaseImplError(
        f"unknown phase implementation {uses!r}. Expected 'local', 'job', "
        f"a path like './phases/{uses}', or one of the shared phases: "
        f"{', '.join(shared_names()) or '(none)'}"
    )
