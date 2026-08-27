"""Mirroring the campaign directory to a durability tier.

Local disk is the working tier; the remote filesystem is a lagging mirror
that nothing reads in normal operation. A background task copies new bytes
every k seconds, and a job launch pushes immediately rather than waiting -
so the worst case after losing the local disk is k seconds of bookkeeping,
never a running job (§6.2).

The remote FS is reached through a small `Mirror` interface: a local
directory implementation ships for development and CI, and a real remote
filesystem is another implementation of the same three methods.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class Mirror(Protocol):
    """Where a campaign is mirrored. Deliberately tiny: append bytes to a
    file, replace a file wholesale, list what is already there."""

    def append(self, rel_path: str, data: bytes, offset: int) -> None:
        ...

    def put(self, rel_path: str, data: bytes) -> None:
        ...

    def size(self, rel_path: str) -> int:
        """Bytes already mirrored; 0 when the file isn't there yet."""


class DirectoryMirror:
    """A mirror backed by a directory - a local path for development, or a
    mounted remote filesystem in production."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, rel_path: str) -> Path:
        path = self.root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def append(self, rel_path: str, data: bytes, offset: int) -> None:
        path = self._path(rel_path)
        with open(path, "r+b" if path.exists() else "wb") as fh:
            fh.seek(offset)
            fh.write(data)
            fh.truncate()

    def put(self, rel_path: str, data: bytes) -> None:
        path = self._path(rel_path)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def size(self, rel_path: str) -> int:
        path = self.root / rel_path
        return path.stat().st_size if path.exists() else 0


@dataclass
class SyncStats:
    passes: int = 0
    log_bytes: int = 0
    files: int = 0
    failures: int = 0
    last_error: str = ""


class CampaignSync:
    """Incremental mirroring of one campaign directory.

    The event log is append-only, so mirroring it means copying the bytes
    added since last time. Everything else is small or immutable, so a
    changed-since check is enough. A failed pass is not fatal: the next pass
    catches up, because every operation is idempotent.
    """

    LOG_REL = "ledger/events.jsonl"

    def __init__(
        self,
        campaign_dir: str | Path,
        mirror: Mirror,
        interval_s: float = 30.0,
    ):
        self.dir = Path(campaign_dir)
        self.mirror = mirror
        self.interval_s = interval_s
        self.stats = SyncStats()
        self._seen: dict[str, tuple[float, int]] = {}
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()

    # -- one pass ------------------------------------------------------------

    def sync_log(self) -> int:
        """Append the log's new bytes. Returns how many were copied."""
        local = self.dir / self.LOG_REL
        if not local.exists():
            return 0
        offset = self.mirror.size(self.LOG_REL)
        local_size = local.stat().st_size
        if local_size <= offset:
            return 0
        with open(local, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
        self.mirror.append(self.LOG_REL, data, offset)
        self.stats.log_bytes += len(data)
        return len(data)

    def _changed_files(self):
        for path in sorted(self.dir.rglob("*")):
            if not path.is_file():
                continue
            rel = str(path.relative_to(self.dir))
            if rel == self.LOG_REL or rel.endswith(".tmp"):
                continue
            stat = path.stat()
            fingerprint = (stat.st_mtime, stat.st_size)
            if self._seen.get(rel) == fingerprint:
                continue
            yield rel, path, fingerprint

    def sync_files(self) -> int:
        copied = 0
        for rel, path, fingerprint in self._changed_files():
            self.mirror.put(rel, path.read_bytes())
            self._seen[rel] = fingerprint
            copied += 1
        self.stats.files += copied
        return copied

    def sync_once(self) -> tuple[int, int]:
        """One full pass. Never raises: a mirror hiccup must not stop
        research, and the next pass will catch up."""
        self.stats.passes += 1
        try:
            return self.sync_log(), self.sync_files()
        except OSError as exc:
            self.stats.failures += 1
            self.stats.last_error = str(exc)
            return 0, 0

    # -- immediate pushes ----------------------------------------------------

    def push_now(self) -> None:
        """Mirror the log immediately - used when a job_id has just been
        recorded and must not wait for the next pass."""
        try:
            self.sync_log()
        except OSError as exc:
            self.stats.failures += 1
            self.stats.last_error = str(exc)

    # -- background task -----------------------------------------------------

    async def _run(self) -> None:
        try:
            while True:
                self.sync_once()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self.interval_s)
                    self._wake.clear()
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            self.sync_once()  # final flush on shutdown
            raise

    def start(self) -> asyncio.Task:
        self._task = asyncio.create_task(self._run())
        return self._task

    def nudge(self) -> None:
        """Ask the background task to run a pass now."""
        self._wake.set()

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None


def restore(mirror_root: str | Path, campaign_dir: str | Path) -> Path:
    """Disaster recovery: copy a mirrored campaign back to local disk. The
    engine then opens it exactly as it would any campaign - replay, heal
    stale views, reattach to in-flight jobs."""
    src, dst = Path(mirror_root), Path(campaign_dir)
    if dst.exists():
        raise FileExistsError(f"refusing to restore over an existing {dst}")
    shutil.copytree(src, dst)
    return dst
