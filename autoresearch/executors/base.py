"""Executor results and the idempotency key. Docs 04, 06."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

from ..store.db import short


@dataclass
class Status:
    state: str                 # RUNNING | COMPLETED | FAILED
    failure_class: str | None = None    # 'infra' | 'experiment'
    detail: str = ""


def idem_key(campaign_id: str, experiment_id: str, stage_key: str, attempt: int) -> str:
    """Deterministic and derivable from state alone.

    Structured rather than hashed so it is greppable in the job system: an
    operator looking at a running job can see which experiment it belongs to
    without consulting the ledger. The `ar-` prefix is what marks a job as
    ours, and the orphan sweep considers nothing else.
    """
    return f"ar-{short(campaign_id)}-{short(experiment_id)}-{stage_key}-{attempt}"


def receipt_path(artifact_dir: str, key: str) -> str:
    return os.path.join(artifact_dir, "launch", f"{key}.xid")


def run_command(cmd: str | list[str], env: dict[str, str], timeout: int,
                cwd: str | None = None) -> subprocess.CompletedProcess:
    full_env = {**os.environ, **env}
    return subprocess.run(
        cmd, shell=isinstance(cmd, str), env=full_env, cwd=cwd,
        capture_output=True, text=True, timeout=timeout,
    )
