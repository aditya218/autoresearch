"""Append-only JSONL event ledger - the campaign's source of truth.

Single writer (the orchestrator). Append = validate, write one JSON line,
fsync. Opening recovers from a crash mid-write by truncating an unterminated
final line - the standard write-ahead-log technique. Any *terminated* line
that fails to parse, or a gap in sequence numbers, is real corruption and
raises rather than being silently dropped.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator, Type, TypeVar

from autoresearch.events import BaseEvent, Event, dump_event, parse_event, utc_now

E = TypeVar("E", bound=BaseEvent)


class LedgerError(Exception):
    """The ledger file is corrupt (bad line or sequence gap)."""


class Ledger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._last_seq = 0
        #: bytes dropped from a torn final line during recovery (0 = clean open)
        self.recovered_bytes = 0
        self._recover()
        self._fh = open(self.path, "ab")

    # -- recovery ------------------------------------------------------------

    def _recover(self) -> None:
        if not self.path.exists():
            return
        data = self.path.read_bytes()
        last_seq = 0
        pos = 0
        while True:
            nl = data.find(b"\n", pos)
            if nl == -1:
                break  # anything left is an unterminated tail
            line = data[pos : nl]
            try:
                event = parse_event(line)
            except Exception as exc:
                raise LedgerError(
                    f"{self.path}: corrupt event line at byte {pos}: {exc}"
                ) from exc
            if event.seq != last_seq + 1:
                raise LedgerError(
                    f"{self.path}: sequence gap: expected {last_seq + 1}, "
                    f"found {event.seq} at byte {pos}"
                )
            last_seq = event.seq
            pos = nl + 1
        if pos < len(data):
            # Torn final write from a crash: truncate it.
            self.recovered_bytes = len(data) - pos
            with open(self.path, "r+b") as fh:
                fh.truncate(pos)
        self._last_seq = last_seq

    # -- writing -------------------------------------------------------------

    def append(self, event_cls: Type[E], **fields) -> E:
        """Create, validate, durably append one event; returns it with its
        assigned seq. The event is on disk (fsynced) when this returns."""
        event = event_cls(seq=self._last_seq + 1, ts=utc_now(), **fields)
        payload = (dump_event(event) + "\n").encode()
        self._fh.write(payload)
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._last_seq = event.seq
        return event

    # -- reading -------------------------------------------------------------

    def events(self) -> Iterator[Event]:
        """Iterate every event currently in the log, in order."""
        with open(self.path, "rb") as fh:
            for line in fh:
                if line.endswith(b"\n"):
                    yield parse_event(line)

    @property
    def last_seq(self) -> int:
        return self._last_seq

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
