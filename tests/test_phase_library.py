"""Resolving `uses:` - shared phases, custom directories, and typos."""

from pathlib import Path

import pytest

from autoresearch.phase_library import (
    PhaseImplError,
    resolve,
    shared_names,
)

SHARED = Path(__file__).resolve().parent.parent / "autoresearch" / "shared_phases"


def test_project_forms_resolve_to_the_project(tmp_path):
    assert resolve("job", tmp_path).scripts_dir == tmp_path
    assert resolve("job", tmp_path).is_job is True
    assert resolve("local", tmp_path).is_job is False


def test_shared_slurm_phase_ships_with_the_engine():
    assert "slurm_job" in shared_names()
    impl = resolve("slurm_job", "/nonexistent-project")
    assert impl.is_job
    assert impl.scripts_dir == SHARED / "slurm_job"


def test_shared_slurm_phase_provides_the_whole_contract():
    """launch/poll/collect are required; find and cancel are what make an
    ambiguous launch and a killed trial recoverable."""
    d = SHARED / "slurm_job"
    for script in ("launch", "poll", "collect", "find", "cancel"):
        path = d / script
        assert path.exists(), f"shared slurm_job is missing {script}"
        assert path.stat().st_mode & 0o111, f"{script} is not executable"


def test_custom_directory_resolves_relative_to_the_project(tmp_path):
    custom = tmp_path / "phases" / "my_sim"
    custom.mkdir(parents=True)
    (custom / "run").write_text("#!/bin/sh\n")
    impl = resolve("./phases/my_sim", tmp_path)
    assert impl.scripts_dir == custom
    assert impl.is_job is False


def test_custom_directory_kind_follows_its_scripts(tmp_path):
    custom = tmp_path / "phases" / "cluster"
    custom.mkdir(parents=True)
    for name in ("launch", "poll", "collect"):
        (custom / name).write_text("#!/bin/sh\n")
    assert resolve("./phases/cluster", tmp_path).is_job is True


def test_a_typo_is_an_error_not_a_silent_local_phase(tmp_path):
    """This used to fall through to the project's `run` script, so a typo
    ran the wrong thing without complaint."""
    with pytest.raises(PhaseImplError, match="unknown phase implementation"):
        resolve("slurm-job", tmp_path)


def test_the_error_lists_what_is_available(tmp_path):
    with pytest.raises(PhaseImplError, match="slurm_job"):
        resolve("nonsense", tmp_path)


def test_missing_custom_directory_is_reported(tmp_path):
    with pytest.raises(PhaseImplError, match="not found"):
        resolve("./phases/ghost", tmp_path)


def test_directory_without_scripts_is_reported(tmp_path):
    empty = tmp_path / "phases" / "empty"
    empty.mkdir(parents=True)
    with pytest.raises(PhaseImplError, match="neither a job phase"):
        resolve("./phases/empty", tmp_path)
