"""External-job executor: user-supplied shell commands (D10).

The engine's entire knowledge of the infrastructure is `launch`, `poll`, and
optionally `find` and `logs`, plus a metrics file. It never learns what the job
actually does (D9).
"""
from __future__ import annotations

import os
import subprocess

from .base import Status, receipt_path, run_command

# One map, raw job status -> what it means to the engine (doc 06). The
# vocabulary deliberately includes the failure class, because a job system that
# distinguishes "preempted" from "failed" already knows the thing the engine
# most needs and cannot otherwise infer.
#
#   success    -> COMPLETED
#   running    -> still going
#   infra      -> FAILED, not a research result: retry
#   experiment -> FAILED, and that IS the result: do not retry
#   triage     -> FAILED, ambiguous: fall through to the retry ceiling
DEFAULT_STATUS_MAP = {
    "PENDING": "running",
    "QUEUED": "running",
    "RUNNING": "running",
    "SUCCEEDED": "success",
    "COMPLETED": "success",
    "PREEMPTED": "infra",
    "KILLED": "infra",
    "QUOTA_EXCEEDED": "infra",
    "NODE_FAILURE": "infra",
    "FAILED": "triage",
    "ERROR": "triage",
}
MEANINGS = {"success", "running", "infra", "experiment", "triage"}


class CommandExecutor:
    kind = "external_job"

    def __init__(self, stage, artifact_dir: str, env: dict[str, str]):
        self.stage = stage
        self.artifact_dir = artifact_dir
        self.env = env

    # ---------------------------------------------------------------- launch

    def launch(self, key: str) -> str:
        """Submit the job and return its id.

        The receipt file is written by the launcher BEFORE it exits, so a crash
        between submission and the engine's commit is recoverable (D11 tier 1).
        """
        os.makedirs(os.path.join(self.artifact_dir, "launch"), exist_ok=True)
        env = {
            **self.env,
            "AUTORESEARCH_IDEM_KEY": key,
            "AUTORESEARCH_RECEIPT": receipt_path(self.artifact_dir, key),
            "AUTORESEARCH_ARTIFACT_DIR": self.artifact_dir,
        }
        proc = run_command(self.stage.launch, env, timeout=300)
        if proc.returncode != 0:
            raise RuntimeError(f"launch failed rc={proc.returncode}: {proc.stderr[-800:]}")
        job_id = (proc.stdout.strip().splitlines() or [""])[-1].strip()
        if not job_id:
            raise RuntimeError("launch printed no job id")
        return job_id

    # --------------------------------------------------------------- recover

    def recover(self, key: str) -> str | None:
        """Resolve LAUNCH_INTENT: did a job actually start? (doc 04 §2)

        Tier 1 is the receipt the launcher wrote. Tier 2 is `find`, which also
        covers a launcher killed mid-submission. Returning None means no job
        exists and it is safe to launch.
        """
        path = receipt_path(self.artifact_dir, key)
        if os.path.exists(path):
            job_id = open(path).read().strip()
            if job_id:
                return job_id

        if self.stage.find:
            env = {**self.env, "AUTORESEARCH_IDEM_KEY": key}
            try:
                proc = run_command(self.stage.find, env, timeout=120)
            except subprocess.TimeoutExpired:
                return None
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout.strip().splitlines()[-1].strip()
        return None

    # ------------------------------------------------------------------ poll

    def poll(self, job_id: str) -> Status:
        """Cheap, idempotent, side-effect free. Runs every tick per in-flight stage."""
        env = {**self.env, "AUTORESEARCH_JOB_ID": job_id}
        cmd = self.stage.poll.replace("{{ job_id }}", job_id).replace("{{job_id}}", job_id)
        try:
            proc = run_command(cmd, env, timeout=120)
        except subprocess.TimeoutExpired:
            # A poll timeout says nothing about the job. Keep waiting.
            return Status("RUNNING", detail="poll timed out")
        if proc.returncode != 0:
            return Status("RUNNING", detail=f"poll rc={proc.returncode}")

        raw = (proc.stdout.strip().splitlines() or [""])[-1].strip().upper()
        meaning = self.stage.status_map.get(raw) or DEFAULT_STATUS_MAP.get(raw)

        if meaning not in MEANINGS:
            # Unknown status: assume the job is alive rather than inventing a
            # verdict. A stage timeout is what bounds this, not a guess here.
            return Status("RUNNING", detail=f"unknown status {raw!r}")
        if meaning == "running":
            return Status("RUNNING", detail=raw)
        if meaning == "success":
            return Status("COMPLETED", detail=raw)

        # A stage-level failure_class override wins; otherwise the map decides;
        # 'triage' means genuinely ambiguous and defers to the retry ceiling.
        cls = self.stage.failure_class or (None if meaning == "triage" else meaning)
        return Status("FAILED", failure_class=cls, detail=raw)

    def logs(self, job_id: str, limit: int = 4000) -> str:
        if not self.stage.logs:
            return ""
        cmd = self.stage.logs.replace("{{ job_id }}", job_id).replace("{{job_id}}", job_id)
        try:
            proc = run_command(cmd, {**self.env, "AUTORESEARCH_JOB_ID": job_id}, timeout=120)
        except subprocess.TimeoutExpired:
            return ""
        return proc.stdout[-limit:]
