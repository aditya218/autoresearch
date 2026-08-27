"""The hg adapter's command surface.

hg is the primary target but is not installed on every dev machine, so these
tests drive the adapter against a fake `hg` that records its arguments and
mimics just enough behaviour. They pin *which commands the engine issues* -
the part that has to be right for a real hg deployment.
"""

import json
from pathlib import Path

import pytest

from autoresearch.vcs import HgVCS, WorkspaceClientVCS

FAKE = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

log = Path(os.environ["FAKE_VCS_LOG"])
argv = sys.argv[1:]
calls = json.loads(log.read_text()) if log.exists() else []
calls.append({"argv": argv, "cwd": os.getcwd()})
log.write_text(json.dumps(calls))

cmd = argv[0] if argv else ""
if cmd == "share":          # share <repo> <dest>: make the working dir
    Path(argv[2]).mkdir(parents=True, exist_ok=True)
    (Path(argv[2]) / "model.py").write_text("lr = 0.1\\n")
elif cmd == "workspace":    # <client> workspace create <dest>
    Path(argv[2]).mkdir(parents=True, exist_ok=True)
elif cmd == "log":
    print("abc123def456")
sys.exit(0)
'''


@pytest.fixture
def fake_hg(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls.json"
    for name in ("hg", "wsclient"):
        path = bindir / name
        path.write_text(FAKE)
        path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{__import__('os').environ['PATH']}")
    monkeypatch.setenv("FAKE_VCS_LOG", str(calls))

    def read():
        return json.loads(calls.read_text()) if calls.exists() else []

    return read


def argv_of(calls, verb):
    return [c["argv"] for c in calls if c["argv"] and c["argv"][0] == verb]


def test_acquire_shares_then_updates_to_the_node(tmp_path, fake_hg):
    repo = tmp_path / "repo"
    repo.mkdir()
    vcs = HgVCS(repo, tmp_path / "ws")
    vcs.workspace_acquire("deadbeef", "T001")

    calls = fake_hg()
    assert argv_of(calls, "share"), "expected an `hg share` for the workspace"
    updates = argv_of(calls, "update")
    assert ["update", "--clean", "-r", "deadbeef"] in updates


def test_acquire_without_a_node_uses_tip(tmp_path, fake_hg):
    repo = tmp_path / "repo"
    repo.mkdir()
    HgVCS(repo, tmp_path / "ws").workspace_acquire(None, "T001")
    assert ["update", "--clean", "-r", "tip"] in argv_of(fake_hg(), "update")


def test_commit_uses_dash_A_and_returns_the_node(tmp_path, fake_hg):
    """No staging area: `hg commit -A` takes everything in the workspace."""
    repo = tmp_path / "repo"
    repo.mkdir()
    vcs = HgVCS(repo, tmp_path / "ws")
    ws = vcs.workspace_acquire(None, "T001")
    node = vcs.commit_all(ws, "T001: bigger lr")

    commits = argv_of(fake_hg(), "commit")
    assert commits == [["commit", "-A", "-m", "T001: bigger lr"]]
    assert node == "abc123def456"  # read back via `hg log -T {node}`


def test_current_node_asks_for_the_full_hash(tmp_path, fake_hg):
    repo = tmp_path / "repo"
    repo.mkdir()
    vcs = HgVCS(repo, tmp_path / "ws")
    ws = vcs.workspace_acquire(None, "T001")
    assert vcs.current_node(ws) == "abc123def456"
    assert ["log", "-r", ".", "-T", "{node}"] in argv_of(fake_hg(), "log")


def test_release_cleans_and_pools(tmp_path, fake_hg):
    repo = tmp_path / "repo"
    repo.mkdir()
    vcs = HgVCS(repo, tmp_path / "ws", pool=True)
    ws = vcs.workspace_acquire(None, "T001")
    vcs.workspace_release(ws)

    calls = fake_hg()
    assert argv_of(calls, "purge"), "a pooled workspace must be purged before reuse"
    # Reusing it must not create a second share.
    before = len(argv_of(calls, "share"))
    vcs.workspace_acquire(None, "T002")
    assert len(argv_of(fake_hg(), "share")) == before


def test_unpooled_release_removes_the_workspace(tmp_path, fake_hg):
    repo = tmp_path / "repo"
    repo.mkdir()
    vcs = HgVCS(repo, tmp_path / "ws", pool=False)
    ws = vcs.workspace_acquire(None, "T001")
    assert ws.path.exists()
    vcs.workspace_release(ws)
    assert not ws.path.exists()


def test_diff_between_two_nodes(tmp_path, fake_hg):
    repo = tmp_path / "repo"
    repo.mkdir()
    HgVCS(repo, tmp_path / "ws").diff("aaa", "bbb")
    assert ["diff", "-r", "aaa", "-r", "bbb"] in argv_of(fake_hg(), "diff")


def test_workspace_client_differs_only_in_creation(tmp_path, fake_hg):
    repo = tmp_path / "repo"
    repo.mkdir()
    vcs = WorkspaceClientVCS(repo, tmp_path / "ws", binary="wsclient")
    ws = vcs.workspace_acquire("deadbeef", "T001")
    vcs.commit_all(ws, "msg")

    calls = fake_hg()
    assert argv_of(calls, "workspace")  # <client> workspace create ...
    assert not argv_of(calls, "share")  # not hg's share
    # Everything after creation is the same command surface as hg.
    assert ["update", "--clean", "-r", "deadbeef"] in argv_of(calls, "update")
    assert ["commit", "-A", "-m", "msg"] in argv_of(calls, "commit")
