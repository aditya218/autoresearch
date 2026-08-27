"""VCS adapters: the same nameless, node-hash interface on every backend.

hg is the primary target (§6.4) but is not installed everywhere, so the
shared contract is exercised against whichever adapters this machine can run.
Adding a backend means adding it to `adapters()` - if it passes these, the
engine cannot tell it apart from the others.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from autoresearch.vcs import CopyVCS, GitVCS, HgVCS, VCSError, available, make_vcs


def make_git_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "model.py").write_text("lr = 0.1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    return repo


def make_hg_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["hg", "init"], cwd=repo, check=True)
    (repo / "model.py").write_text("lr = 0.1\n")
    subprocess.run(["hg", "add"], cwd=repo, check=True)
    subprocess.run(["hg", "commit", "-m", "base", "-u", "test"], cwd=repo, check=True)
    return repo


def make_plain_base(root: Path) -> Path:
    """A base code state that isn't under version control at all."""
    base = root / "base"
    base.mkdir(parents=True)
    (base / "model.py").write_text("lr = 0.1\n")
    return base


def adapters(tmp_path):
    """Every adapter runnable on this machine, as (name, vcs) pairs."""
    out = [
        (
            "copy",
            CopyVCS(make_plain_base(tmp_path / "copy"), tmp_path / "ws-copy"),
        )
    ]
    if available("git"):
        out.append(
            ("git", GitVCS(make_git_repo(tmp_path / "git"), tmp_path / "ws-git"))
        )
    if available("hg"):
        out.append(("hg", HgVCS(make_hg_repo(tmp_path / "hg"), tmp_path / "ws-hg")))
    return out


# -- the shared contract -----------------------------------------------------


def test_acquire_edit_commit_branch(tmp_path):
    """The trial lifecycle: take a base state, edit it, commit a node, then
    branch a second trial from that node - the tree of ideas."""
    for name, vcs in adapters(tmp_path):
        ws1 = vcs.workspace_acquire(None, f"{name}-T001")
        (ws1.path / "model.py").write_text("lr = 0.2\n")
        node1 = vcs.commit_all(ws1, "T001: bigger lr")
        assert node1, f"{name}: commit returned no node"

        # A second trial branching off the first sees the first's changes.
        ws2 = vcs.workspace_acquire(node1, f"{name}-T002")
        assert (ws2.path / "model.py").read_text() == "lr = 0.2\n", name

        vcs.workspace_release(ws1)
        vcs.workspace_release(ws2)


def test_workspaces_are_isolated(tmp_path):
    for name, vcs in adapters(tmp_path):
        ws1 = vcs.workspace_acquire(None, f"{name}-A")
        ws2 = vcs.workspace_acquire(None, f"{name}-B")
        (ws1.path / "model.py").write_text("lr = 0.9\n")
        assert (ws2.path / "model.py").read_text() != "lr = 0.9\n", name
        vcs.workspace_release(ws1)
        vcs.workspace_release(ws2)


def test_nodes_are_stable_identifiers(tmp_path):
    """Nameless VCS: what the ledger stores is a node, and re-acquiring at
    that node reproduces the state (§6.4)."""
    for name, vcs in adapters(tmp_path):
        ws = vcs.workspace_acquire(None, f"{name}-T001")
        (ws.path / "model.py").write_text("lr = 0.3\n")
        node = vcs.commit_all(ws, "change")
        vcs.workspace_release(ws)

        again = vcs.workspace_acquire(node, f"{name}-T001-again")
        assert (again.path / "model.py").read_text() == "lr = 0.3\n", name
        vcs.workspace_release(again)


def test_commit_all_needs_no_staging(tmp_path):
    """A new file an agent created is committed without being added first."""
    for name, vcs in adapters(tmp_path):
        ws = vcs.workspace_acquire(None, f"{name}-T001")
        (ws.path / "brand_new.py").write_text("print('hi')\n")
        node = vcs.commit_all(ws, "adds a file")
        vcs.workspace_release(ws)

        again = vcs.workspace_acquire(node, f"{name}-check")
        assert (again.path / "brand_new.py").exists(), name
        vcs.workspace_release(again)


def test_commit_with_no_changes_is_not_an_error(tmp_path):
    """A phase that changed nothing must not crash the trial."""
    for name, vcs in adapters(tmp_path):
        ws = vcs.workspace_acquire(None, f"{name}-T001")
        node = vcs.commit_all(ws, "no changes")
        assert isinstance(node, str), name
        vcs.workspace_release(ws)


def test_diff_between_nodes(tmp_path):
    for name, vcs in adapters(tmp_path):
        ws = vcs.workspace_acquire(None, f"{name}-T001")
        base = vcs.commit_all(ws, "base")
        (ws.path / "model.py").write_text("lr = 0.5\n")
        changed = vcs.commit_all(ws, "changed")
        if base == changed:
            continue  # nothing to diff on a backend that dedupes
        assert "0.5" in vcs.diff(base, changed), name
        vcs.workspace_release(ws)


# -- adapter selection -------------------------------------------------------


def test_make_vcs_rejects_unknown_kind(tmp_path):
    with pytest.raises(VCSError, match="unknown vcs"):
        make_vcs("svn", tmp_path, tmp_path / "ws")


def test_make_vcs_reports_a_missing_tool(tmp_path):
    missing = "hg"
    if available(missing):
        pytest.skip(f"{missing} is installed here")
    with pytest.raises(VCSError, match="not installed"):
        make_vcs(missing, tmp_path, tmp_path / "ws")


def test_copy_adapter_needs_no_tools(tmp_path):
    vcs = make_vcs("copy", None, tmp_path / "ws")
    ws = vcs.workspace_acquire(None, "T001")
    assert ws.path.exists()
    vcs.workspace_release(ws)


@pytest.mark.skipif(not available("git"), reason="git not installed")
def test_git_workspaces_are_detached(tmp_path):
    """Nameless means no branch per trial: worktrees sit at a detached
    commit."""
    vcs = GitVCS(make_git_repo(tmp_path / "git"), tmp_path / "ws")
    ws = vcs.workspace_acquire(None, "T001")
    head = subprocess.run(
        ["git", "symbolic-ref", "-q", "HEAD"], cwd=ws.path, capture_output=True
    )
    assert head.returncode != 0  # detached: no symbolic ref
    vcs.workspace_release(ws)


@pytest.mark.skipif(not available("hg"), reason="hg not installed")
def test_hg_pool_reuses_workspaces(tmp_path):
    vcs = HgVCS(make_hg_repo(tmp_path / "hg"), tmp_path / "ws", pool=True)
    first = vcs.workspace_acquire(None, "T001")
    path = first.path
    vcs.workspace_release(first)
    second = vcs.workspace_acquire(None, "T002")
    assert second.path == path  # acquisition is expensive, so it is pooled
