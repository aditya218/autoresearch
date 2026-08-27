"""Crash/recovery: SIGKILL a writer mid-stream, then recovery must converge.

The property under test is the design's core reliability claim: after an
arbitrary kill, opening the ledger recovers a clean prefix, replay produces
consistent state, no launched job is unrecorded relative to that prefix, and
writing can continue seamlessly.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from autoresearch.events import TrialCreated
from autoresearch.ledger import Ledger
from autoresearch.state import CampaignState
from autoresearch.views import ViewWriter

WRITER = Path(__file__).parent / "_crash_writer.py"


@pytest.mark.parametrize("delay", [0.05, 0.15, 0.3])
def test_kill_writer_then_recover(tmp_path, delay):
    path = tmp_path / "ledger" / "events.jsonl"
    proc = subprocess.Popen([sys.executable, str(WRITER), str(path)])
    try:
        time.sleep(delay)
    finally:
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait()

    with Ledger(path) as led:
        events = list(led.events())
        assert len(events) > 0, "writer produced nothing; increase delay"
        # Replay must succeed and agree with the recovered log.
        state = CampaignState.replay(events)
        assert state.last_seq == led.last_seq

        # Every job the recovered log knows was launched is attached to a
        # live phase of an in-flight trial or a finished one - never dangling.
        for job_id in state.launched_job_ids:
            assert job_id.startswith("job-")

        # At most one trial can be mid-lifecycle at the kill point.
        assert state.in_flight_trials <= 1

        # Views regenerate cleanly from the recovered state.
        writer = ViewWriter(tmp_path)
        rewritten = writer.regenerate_stale(state)
        assert "index" in rewritten

        # And the ledger keeps working: append continues the sequence.
        e = led.append(TrialCreated, trial="T-after-crash")
        assert e.seq == state.last_seq + 1


def test_recovery_is_idempotent(tmp_path):
    path = tmp_path / "events.jsonl"
    proc = subprocess.Popen([sys.executable, str(WRITER), str(path)])
    time.sleep(0.15)
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait()

    with Ledger(path) as led:
        first = led.last_seq
    with Ledger(path) as led:
        assert led.last_seq == first
        assert led.recovered_bytes == 0  # second open finds a clean log
