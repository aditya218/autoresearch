"""The project interface: scripts the engine runs, knowing nothing about them.

A project supplies executables in its project directory. The engine's whole
contract with a remote job system is three scripts, plus an optional fourth:

    launch  <phase> --tag TAG --workspace DIR --out DIR   -> prints job_id
    poll    <phase> --job-id ID                           -> prints status
    collect <phase> --job-id ID --out DIR                 -> writes result.json
    find    <phase> --tag TAG                             -> prints job_ids

Statuses the engine understands: pending | running | done | failed. Anything
else is an unknown status - a no-rule situation, which is where repair comes
in (§9.1).

`pending` means the job is queued and has consumed no compute yet, which is
worth distinguishing: a job that has never started may never schedule, and
the answer is usually to resubmit it smaller rather than to keep waiting.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

PENDING = "pending"   # queued; nothing has started
RUNNING = "running"   # work is actually happening
DONE = "done"
FAILED = "failed"
KNOWN_STATUSES = frozenset({PENDING, RUNNING, DONE, FAILED})
#: statuses meaning "not finished yet"
LIVE_STATUSES = frozenset({PENDING, RUNNING})


class ScriptError(Exception):
    """A project script failed or answered incomprehensibly."""


@dataclass
class ScriptResult:
    returncode: int
    stdout: str
    stderr: str


class Project:
    """Runs a project's scripts. Scripts live outside the trial workspace, so
    an agent editing training code cannot rewrite the eval harness (§7.3)."""

    def __init__(self, project_dir: str | Path):
        self.dir = Path(project_dir)

    def script_path(self, name: str) -> Path:
        return self.dir / name

    def has(self, name: str) -> bool:
        return self.script_path(name).exists()

    def run(
        self,
        name: str,
        args: list[str],
        timeout_s: float | None = None,
        cwd: str | Path | None = None,
    ) -> ScriptResult:
        path = self.script_path(name)
        if not path.exists():
            raise ScriptError(f"project script not found: {path}")
        try:
            proc = subprocess.run(
                [str(path), *args],
                capture_output=True,
                text=True,
                timeout=timeout_s,
                cwd=str(cwd) if cwd else None,
            )
        except subprocess.TimeoutExpired as exc:
            raise ScriptError(f"{path}: timed out after {timeout_s}s") from exc
        except OSError as exc:
            raise ScriptError(f"{path}: cannot execute: {exc}") from exc
        return ScriptResult(proc.returncode, proc.stdout, proc.stderr)

    # -- the job contract ----------------------------------------------------

    def launch(
        self, phase: str, tag: str, workspace: Path, out_dir: Path,
        params: dict | None = None, timeout_s: float | None = None,
    ) -> str:
        """Launch a remote job; returns its job_id.

        An exit code of 0 with no parseable job_id is an *ambiguous launch*:
        a job may or may not exist. That is exactly what `find` (and repair)
        are for, so it is reported as a distinct condition.
        """
        args = [phase, "--tag", tag, "--workspace", str(workspace), "--out", str(out_dir)]
        for key, value in (params or {}).items():
            args += [f"--{key}", str(value)]
        res = self.run("launch", args, timeout_s=timeout_s)
        job_id = res.stdout.strip().splitlines()[-1].strip() if res.stdout.strip() else ""
        if res.returncode != 0 or not job_id:
            raise ScriptError(
                f"launch {phase}: exit {res.returncode}, job_id={job_id!r}\n"
                f"{res.stderr.strip()}"
            )
        return job_id

    def poll(self, phase: str, job_id: str, timeout_s: float | None = None) -> str:
        res = self.run("poll", [phase, "--job-id", job_id], timeout_s=timeout_s)
        if res.returncode != 0:
            raise ScriptError(
                f"poll {phase} {job_id}: exit {res.returncode}\n{res.stderr.strip()}"
            )
        return res.stdout.strip().splitlines()[-1].strip() if res.stdout.strip() else ""

    def collect(
        self, phase: str, job_id: str, out_dir: Path, timeout_s: float | None = None
    ) -> None:
        res = self.run(
            "collect", [phase, "--job-id", job_id, "--out", str(out_dir)],
            timeout_s=timeout_s,
        )
        if res.returncode != 0:
            raise ScriptError(
                f"collect {phase} {job_id}: exit {res.returncode}\n{res.stderr.strip()}"
            )

    def find(self, phase: str, tag: str, timeout_s: float | None = None) -> list[str]:
        """Optional: look up jobs by tag, turning an ambiguous launch into a
        lookup. Absent script -> no answer, not an error."""
        if not self.has("find"):
            return []
        res = self.run("find", [phase, "--tag", tag], timeout_s=timeout_s)
        if res.returncode != 0:
            raise ScriptError(
                f"find {phase} {tag}: exit {res.returncode}\n{res.stderr.strip()}"
            )
        return [line.strip() for line in res.stdout.splitlines() if line.strip()]
