"""Engine-mediated tools for agentic phases.

In freeform mode a single agentic phase does everything, including launching
remote jobs. If the agent launched them directly, a crash between launch and
report would orphan a job - exactly what the ledger exists to prevent. So the
engine proxies the launch: the project's script runs, and the job_id is
durably recorded before the agent is told about it (§5.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from autoresearch import events as ev
from autoresearch.project import DONE, FAILED, Project, ScriptError


@dataclass
class JobTools:
    """`launch_job` / `poll_job` / `collect_job`, bound to one trial+phase."""

    project: Project
    recorder: object  # engine.Recorder
    trial_id: str
    phase: str
    workspace: Path
    phase_dir: Path
    campaign: str = ""

    def _tag(self, suffix: str = "") -> str:
        return f"{self.campaign}/{self.trial_id}/{self.phase}{suffix}"

    def launch_job(self, params: dict | None = None, suffix: str = "") -> str:
        """Launch a job and record it before returning. The agent never holds
        an unrecorded job_id."""
        job_id = self.project.launch(
            self.phase, self._tag(suffix), self.workspace, self.phase_dir,
            params=params or {},
        )
        self.recorder.record(
            ev.JobLaunched, trial=self.trial_id, phase=self.phase,
            job_id=job_id, tag=self._tag(suffix),
        )
        return job_id

    def poll_job(self, job_id: str) -> str:
        status = self.project.poll(self.phase, job_id)
        self.recorder.record(
            ev.JobStatusChanged, trial=self.trial_id, phase=self.phase,
            job_id=job_id, status=status,
        )
        return status

    def collect_job(self, job_id: str, out_dir: str | Path | None = None) -> str:
        target = Path(out_dir) if out_dir else self.phase_dir
        self.project.collect(self.phase, job_id, target)
        return str(target)

    def as_dict(self) -> dict:
        return {
            "launch_job": self.launch_job,
            "poll_job": self.poll_job,
            "collect_job": self.collect_job,
        }


__all__ = ["JobTools", "DONE", "FAILED", "ScriptError"]
