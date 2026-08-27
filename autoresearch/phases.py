"""Running a single phase.

A phase runner takes a prepared phase directory and a workspace, does the
work, and returns an outcome. It never writes to the ledger - the engine owns
all ledger writes (§7.1). Runners are deliberately dumb about workflow: no
knowledge of gates, retries, trials, or budgets.

Job phases are polled here rather than by a separate loop, because a job's
whole lifetime belongs to its phase; the caller drives it with `step()` so
the engine keeps control of pacing and can persist events between steps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from autoresearch.config import PhaseConfig
from autoresearch.contract import (
    ContractError,
    PhaseResult,
    check_produces,
    read_result,
)
from autoresearch.project import DONE, FAILED, RUNNING, Project, ScriptError


class PhaseFailure(Exception):
    """Infra failure while running a phase: engine-detectable, retryable
    (§9). Distinct from a phase that ran fine and reported `failed`."""


@dataclass
class PhaseOutcome:
    """What the engine records for one phase attempt."""

    status: str  # passed | failed
    metrics: dict[str, float] = field(default_factory=dict)
    notes: str = ""
    verified: bool = False
    job_id: str | None = None


def finalize(
    phase_dir: Path,
    cfg: PhaseConfig,
    verified: bool,
    workspace: Path | None = None,
) -> PhaseOutcome:
    """Read and validate what a phase produced. Contract violations are
    infra failures, not phase results."""
    try:
        result: PhaseResult = read_result(phase_dir)
        check_produces(phase_dir, cfg.produces, workspace)
    except ContractError as exc:
        raise PhaseFailure(str(exc)) from exc
    return PhaseOutcome(
        status=result.status,
        metrics=result.metrics,
        notes=result.notes,
        verified=verified,
    )


# -- deterministic local phases ----------------------------------------------


def run_local_phase(
    project: Project, phase: str, cfg: PhaseConfig, workspace: Path, phase_dir: Path
) -> PhaseOutcome:
    """A phase that runs to completion locally via the project's `run` script."""
    phase_dir.mkdir(parents=True, exist_ok=True)
    args = [phase, "--workspace", str(workspace), "--out", str(phase_dir)]
    for key, value in cfg.params.items():
        args += [f"--{key}", str(value)]
    try:
        res = project.run("run", args, timeout_s=cfg.timeout_s)
    except ScriptError as exc:
        raise PhaseFailure(str(exc)) from exc
    if res.returncode != 0:
        raise PhaseFailure(
            f"run {phase}: exit {res.returncode}\n{res.stderr.strip()}"
        )
    return finalize(phase_dir, cfg, verified=True, workspace=workspace)


# -- remote job phases -------------------------------------------------------


@dataclass
class JobPhase:
    """A remote job phase, driven one step at a time by the engine.

    The engine persists `job_launched` immediately after `launch()` returns,
    before anything else happens - that is what makes a job un-loseable.
    """

    project: Project
    phase: str
    cfg: PhaseConfig
    workspace: Path
    phase_dir: Path
    tag: str
    job_id: str | None = None
    status: str | None = None

    def launch(self) -> str:
        self.phase_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.job_id = self.project.launch(
                self.phase, self.tag, self.workspace, self.phase_dir,
                params=self.cfg.params, timeout_s=self.cfg.timeout_s,
            )
        except ScriptError as exc:
            # Ambiguous launch: ask `find` whether a job exists after all.
            recovered = self._find_by_tag()
            if recovered is None:
                raise PhaseFailure(str(exc)) from exc
            self.job_id = recovered
        self.status = RUNNING
        return self.job_id

    def _find_by_tag(self) -> str | None:
        try:
            found = self.project.find(self.phase, self.tag)
        except ScriptError:
            return None
        return found[0] if len(found) == 1 else None

    def poll(self) -> str:
        """One poll. Returns the raw status; unknown statuses are passed
        through for the engine (and repair) to judge."""
        if self.job_id is None:
            raise PhaseFailure(f"{self.phase}: polled before launch")
        try:
            self.status = self.project.poll(
                self.phase, self.job_id, timeout_s=self.cfg.timeout_s
            )
        except ScriptError as exc:
            raise PhaseFailure(str(exc)) from exc
        return self.status

    @property
    def finished(self) -> bool:
        return self.status in (DONE, FAILED)

    def collect(self) -> PhaseOutcome:
        if self.job_id is None:
            raise PhaseFailure(f"{self.phase}: collected before launch")
        try:
            self.project.collect(
                self.phase, self.job_id, self.phase_dir, timeout_s=self.cfg.timeout_s
            )
        except ScriptError as exc:
            raise PhaseFailure(str(exc)) from exc
        outcome = finalize(
            self.phase_dir, self.cfg, verified=True, workspace=self.workspace
        )
        outcome.job_id = self.job_id
        return outcome
