"""Version control for trial code states.

Designed for Mercurial and hg-compatible clients (§6.4), with three rules that
keep the abstraction thin:

  1. Nameless: code states are immutable commit hashes (nodes), never branch
     names or bookmarks. The ledger already maps trial -> node, so naming
     lives there and this layer only ever speaks hashes.
  2. Workspaces are acquired and released, and acquisition may be expensive -
     so they can be pooled. The engine never assumes it is cheap.
  3. No staging area: committing means committing everything in the
     workspace, which is the right semantic for agent-produced changes.

The hg adapter is primary; git is a secondary for machines without hg, and a
plain-copy adapter backs tests and projects that aren't in version control at
all.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class VCSError(Exception):
    """A version-control operation failed."""


@dataclass
class Workspace:
    path: Path
    node: str | None = None
    #: adapter-private handle, e.g. a pool slot id
    handle: str | None = None


class VCS(Protocol):
    def workspace_acquire(self, base_node: str | None, name: str) -> Workspace: ...
    def commit_all(self, workspace: Workspace, message: str) -> str: ...
    def current_node(self, workspace: Workspace) -> str: ...
    def diff(self, node_a: str, node_b: str) -> str: ...
    def workspace_release(self, workspace: Workspace) -> None: ...


def _run(argv: list[str], cwd: Path | None = None) -> str:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, cwd=str(cwd) if cwd else None
        )
    except OSError as exc:
        raise VCSError(f"cannot run {argv[0]}: {exc}") from exc
    if proc.returncode != 0:
        raise VCSError(
            f"{' '.join(argv)}: exit {proc.returncode}\n{proc.stderr.strip()}"
        )
    return proc.stdout


# -- mercurial and hg-compatible clients -------------------------------------


class HgVCS:
    """Mercurial adapter, using `share` for cheap workspaces.

    Several clients speak Mercurial's command surface but create workspaces
    their own way. Rather than hardcode any of them, the binary is a
    parameter and workspace creation is one overridable method - a deployment
    with its own hg-derived client subclasses this and supplies both, the
    same way projects supply their own launch scripts.
    """

    binary = "hg"

    def __init__(
        self,
        repo: str | Path,
        workspaces_dir: str | Path,
        pool: bool = True,
        binary: str | None = None,
    ):
        self.repo = Path(repo)
        self.workspaces_dir = Path(workspaces_dir)
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)
        self.pool = pool
        if binary:
            self.binary = binary
        self._free: list[Path] = []

    def _hg(self, args: list[str], cwd: Path | None = None) -> str:
        return _run([self.binary, *args], cwd=cwd or self.repo)

    def _create_workspace(self, path: Path) -> None:
        # `share` gives a second working directory over one store: hg's
        # analogue of a git worktree.
        self._hg(["share", str(self.repo), str(path)], cwd=self.repo.parent)

    def workspace_acquire(self, base_node: str | None, name: str) -> Workspace:
        if self.pool and self._free:
            path = self._free.pop()
        else:
            path = self.workspaces_dir / name
            if path.exists():
                shutil.rmtree(path)
            self._create_workspace(path)
        node = base_node or "tip"
        self._hg(["update", "--clean", "-r", node], cwd=path)
        return Workspace(path=path, node=self.current_node(Workspace(path=path)))

    def commit_all(self, workspace: Workspace, message: str) -> str:
        # -A picks up adds and removes: commit everything in the workspace.
        try:
            self._hg(["commit", "-A", "-m", message], cwd=workspace.path)
        except VCSError as exc:
            if "nothing changed" not in str(exc):
                raise
        node = self.current_node(workspace)
        workspace.node = node
        return node

    def current_node(self, workspace: Workspace) -> str:
        return self._hg(["log", "-r", ".", "-T", "{node}"], cwd=workspace.path).strip()

    def diff(self, node_a: str, node_b: str) -> str:
        return self._hg(["diff", "-r", node_a, "-r", node_b])

    def workspace_release(self, workspace: Workspace) -> None:
        if self.pool:
            self._hg(["update", "--clean", "-r", "tip"], cwd=workspace.path)
            self._hg(["purge", "--all"], cwd=workspace.path)
            self._free.append(workspace.path)
        else:
            shutil.rmtree(workspace.path, ignore_errors=True)


class WorkspaceClientVCS(HgVCS):
    """For hg-compatible clients whose workspaces come from a `workspace
    create` command rather than `share`.

    Everything after creation - update, commit -A, log, purge - is Mercurial's
    command surface, which is why this is six lines rather than an adapter.
    Set `binary` to the client's executable.
    """

    def _create_workspace(self, path: Path) -> None:
        self._hg(["workspace", "create", str(path)], cwd=self.repo)


# -- git (secondary) ---------------------------------------------------------


class GitVCS:
    """Git adapter for machines without hg. Nameless too: worktrees are
    checked out at a detached commit, never on a branch."""

    def __init__(self, repo: str | Path, workspaces_dir: str | Path):
        self.repo = Path(repo)
        self.workspaces_dir = Path(workspaces_dir)
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)

    def _git(self, args: list[str], cwd: Path | None = None) -> str:
        return _run(["git", *args], cwd=cwd or self.repo)

    def workspace_acquire(self, base_node: str | None, name: str) -> Workspace:
        path = self.workspaces_dir / name
        if path.exists():
            shutil.rmtree(path)
        node = base_node or self._git(["rev-parse", "HEAD"]).strip()
        self._git(["worktree", "add", "--detach", str(path), node])
        return Workspace(path=path, node=node)

    def commit_all(self, workspace: Workspace, message: str) -> str:
        self._git(["add", "-A"], cwd=workspace.path)
        status = self._git(["status", "--porcelain"], cwd=workspace.path).strip()
        if status:
            self._git(["commit", "-m", message], cwd=workspace.path)
        node = self.current_node(workspace)
        workspace.node = node
        return node

    def current_node(self, workspace: Workspace) -> str:
        return self._git(["rev-parse", "HEAD"], cwd=workspace.path).strip()

    def diff(self, node_a: str, node_b: str) -> str:
        return self._git(["diff", node_a, node_b])

    def workspace_release(self, workspace: Workspace) -> None:
        self._git(["worktree", "remove", "--force", str(workspace.path)])


# -- plain copy (no version control) -----------------------------------------


class CopyVCS:
    """For projects not under version control, and for tests: a workspace is
    a copy of a base directory, and a "node" is a snapshot kept aside.

    It satisfies the same interface, which is the point - the engine cannot
    tell the difference.
    """

    def __init__(self, base_dir: str | Path | None, workspaces_dir: str | Path):
        self.base_dir = Path(base_dir) if base_dir else None
        self.workspaces_dir = Path(workspaces_dir)
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)
        self.snapshots = self.workspaces_dir / ".snapshots"
        self._counter = 0

    def _snapshot_path(self, node: str) -> Path:
        return self.snapshots / node

    def workspace_acquire(self, base_node: str | None, name: str) -> Workspace:
        path = self.workspaces_dir / name
        if path.exists():
            shutil.rmtree(path)
        source = (
            self._snapshot_path(base_node)
            if base_node and self._snapshot_path(base_node).exists()
            else self.base_dir
        )
        if source is not None and Path(source).exists():
            shutil.copytree(source, path)
        else:
            path.mkdir(parents=True)
        return Workspace(path=path, node=base_node)

    def commit_all(self, workspace: Workspace, message: str) -> str:
        self._counter += 1
        node = f"snap{self._counter:04d}"
        target = self._snapshot_path(node)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(workspace.path, target)
        workspace.node = node
        return node

    def current_node(self, workspace: Workspace) -> str:
        return workspace.node or ""

    def diff(self, node_a: str, node_b: str) -> str:
        a, b = self._snapshot_path(node_a), self._snapshot_path(node_b)
        # `diff` exits 1 precisely when there *is* a difference, so the exit
        # code is not an error here - the output is the answer.
        proc = subprocess.run(
            ["diff", "-ru", str(a), str(b)], capture_output=True, text=True
        )
        if proc.returncode > 1:
            raise VCSError(f"diff failed: {proc.stderr.strip()}")
        return proc.stdout

    def workspace_release(self, workspace: Workspace) -> None:
        shutil.rmtree(workspace.path, ignore_errors=True)


def available(binary: str) -> bool:
    return shutil.which(binary) is not None


def make_vcs(kind: str, repo, workspaces_dir):
    """Pick an adapter by name, with a clear error when the tool is absent."""
    kinds = {"hg": HgVCS, "git": GitVCS, "copy": CopyVCS}
    if kind not in kinds:
        raise VCSError(f"unknown vcs {kind!r}; expected one of {sorted(kinds)}")
    if kind in ("hg", "git") and not available(kind):
        raise VCSError(f"{kind} is not installed on this machine")
    return kinds[kind](repo, workspaces_dir)
