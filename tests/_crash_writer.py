"""Subprocess for the crash test: appends trial lifecycles to a ledger as
fast as possible until SIGKILLed by the parent test."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from autoresearch.events import (  # noqa: E402
    CampaignStarted,
    JobLaunched,
    PhaseCompleted,
    PhaseStarted,
    TrialCreated,
    TrialFinished,
)
from autoresearch.ledger import Ledger  # noqa: E402

led = Ledger(sys.argv[1])
if led.last_seq == 0:
    led.append(CampaignStarted, campaign="crash-test")

i = 0
while True:
    i += 1
    t = f"T{i:05d}"
    led.append(TrialCreated, trial=t)
    led.append(PhaseStarted, trial=t, phase="train")
    led.append(JobLaunched, trial=t, phase="train", job_id=f"job-{i}", tag=t)
    led.append(
        PhaseCompleted, trial=t, phase="train", status="passed",
        metrics={"acc": 0.5}, verified=True,
    )
    led.append(TrialFinished, trial=t, status="completed")
