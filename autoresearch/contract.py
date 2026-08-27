"""The phase contract: `result.json`, and the `produces:` file checks.

A phase communicates with the engine through exactly one file. Anything with
consequences (status, metrics) is validated here; prose (`notes`) is carried
along but never acted on.

Declared output files are checked when the *producing* phase completes, so a
consumer never starts with an expected input missing - there is no situation
in which an agent could "helpfully" fabricate one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

RESULT_FILENAME = "result.json"


class ContractError(Exception):
    """A phase did not honour the contract (missing/invalid result.json, or a
    declared output that isn't there). Treated as a retryable infra error."""


class PhaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["passed", "failed"]
    metrics: dict[str, float] = Field(default_factory=dict)
    notes: str = ""
    artifacts: list[str] = Field(default_factory=list)


def result_path(phase_dir: str | Path) -> Path:
    return Path(phase_dir) / RESULT_FILENAME


def read_result(phase_dir: str | Path) -> PhaseResult:
    path = result_path(phase_dir)
    if not path.exists():
        raise ContractError(f"{path}: phase wrote no {RESULT_FILENAME}")
    try:
        return PhaseResult.model_validate_json(path.read_text())
    except ValidationError as exc:
        raise ContractError(f"{path}: invalid {RESULT_FILENAME}:\n{exc}") from exc
    except Exception as exc:
        raise ContractError(f"{path}: cannot read {RESULT_FILENAME}: {exc}") from exc


def write_result(phase_dir: str | Path, result: PhaseResult) -> Path:
    """Used by the engine's own deterministic phases and by tests; project
    scripts write this file themselves."""
    path = result_path(phase_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result.model_dump_json(indent=2) + "\n")
    return path


def check_produces(
    phase_dir: str | Path, produces: list[str], workspace: str | Path | None = None
) -> None:
    """Verify every declared output exists, at the moment the producer
    completes.

    An output may be an artifact in the phase directory (eval results, logs)
    or a file in the trial workspace (code an implement phase wrote) - both
    are things a later phase reads, so either location satisfies the
    declaration.
    """
    roots = [Path(phase_dir)] + ([Path(workspace)] if workspace is not None else [])
    missing = [
        rel for rel in produces if not any((root / rel).exists() for root in roots)
    ]
    if missing:
        raise ContractError(
            f"{Path(phase_dir)}: phase completed without its declared outputs: "
            + ", ".join(sorted(missing))
        )
