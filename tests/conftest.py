import json
import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TOY_SRC = REPO / "toy_project"


@pytest.fixture
def toy_project(tmp_path) -> Path:
    """A private copy of the toy project, so fake-job state stays per-test."""
    dst = tmp_path / "project"
    shutil.copytree(TOY_SRC, dst)
    return dst


@pytest.fixture
def workspace(tmp_path) -> Path:
    """A trial workspace holding a toy `change.json` (what the implement
    phase would have written)."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "change.json").write_text(json.dumps({"name": "bigger-lr", "delta": 0.2}))
    return ws
