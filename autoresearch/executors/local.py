"""Local executor: cheap work, re-executed from scratch after a crash.

Runs in a subprocess, never in the controller process. In the full design this
is where agent-authored code executes, so the subprocess boundary is a safety
requirement rather than a style choice (doc 08 §1). The prototype does not yet
add container isolation or credential stripping.
"""
from __future__ import annotations

import subprocess

from .base import Status, run_command


class LocalExecutor:
    kind = "local"

    def __init__(self, stage, artifact_dir: str, env: dict[str, str], cwd: str | None = None):
        self.stage = stage
        self.artifact_dir = artifact_dir
        self.env = env
        self.cwd = cwd

    def run(self) -> Status:
        env = {**self.env, "AUTORESEARCH_ARTIFACT_DIR": self.artifact_dir}
        try:
            proc = run_command(self.stage.command, env, timeout=self.stage.timeout, cwd=self.cwd)
        except subprocess.TimeoutExpired:
            return Status("FAILED", failure_class="infra",
                          detail=f"timed out after {self.stage.timeout}s")
        if proc.returncode == 0:
            return Status("COMPLETED", detail=proc.stdout[-2000:])
        return Status(
            "FAILED",
            failure_class=self.stage.failure_class or "experiment",
            detail=(proc.stderr or proc.stdout)[-2000:],
        )
