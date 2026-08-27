import pytest

from autoresearch.events import (
    CampaignFinished,
    CampaignStarted,
    IdeaCreated,
    JobLaunched,
    JobStatusChanged,
    MetricRecorded,
    PhaseCompleted,
    PhaseErrored,
    PhaseStarted,
    TrialCreated,
    TrialFinished,
    TrialReclassified,
)
from autoresearch.ledger import Ledger
from autoresearch.state import CampaignState, StateError


def build(led: Ledger) -> CampaignState:
    return CampaignState.replay(led.events())


def test_full_trial_lifecycle(tmp_path):
    with Ledger(tmp_path / "events.jsonl") as led:
        led.append(CampaignStarted, campaign="c1")
        led.append(IdeaCreated, idea="I1", source="ideator")
        led.append(TrialCreated, trial="T001", idea="I1", base_node="abc123")
        led.append(PhaseStarted, trial="T001", phase="train")
        led.append(JobLaunched, trial="T001", phase="train", job_id="xm-42", tag="c1/T001/train")
        led.append(JobStatusChanged, trial="T001", phase="train", job_id="xm-42", status="running")
        state = build(led)
        assert state.in_flight_trials == 1
        assert state.launched_job_ids == ["xm-42"]
        assert state.backlog == []  # idea consumed

        led.append(
            PhaseCompleted,
            trial="T001",
            phase="train",
            status="passed",
            metrics={"accuracy": 0.91},
            verified=True,
        )
        led.append(TrialFinished, trial="T001", status="completed")
        state = build(led)

    trial = state.trials["T001"]
    assert trial.status == "completed"
    assert trial.metrics["accuracy"].value == 0.91
    assert trial.metrics["accuracy"].verified is True
    assert trial.metrics["accuracy"].phase == "train"
    assert state.in_flight_trials == 0
    assert state.budget_consumed_trials == 1
    assert state.launched_job_ids == []


def test_errored_trial_does_not_consume_budget(tmp_path):
    with Ledger(tmp_path / "events.jsonl") as led:
        led.append(CampaignStarted, campaign="c1")
        led.append(TrialCreated, trial="T001")
        led.append(PhaseStarted, trial="T001", phase="train")
        led.append(PhaseErrored, trial="T001", phase="train", error="boom")
        led.append(TrialFinished, trial="T001", status="errored", reason="boom")
        state = build(led)
    assert state.budget_consumed_trials == 0
    assert state.in_flight_trials == 0


def test_reclassification_corrects_budget(tmp_path):
    with Ledger(tmp_path / "events.jsonl") as led:
        led.append(CampaignStarted, campaign="c1")
        led.append(TrialCreated, trial="T001")
        led.append(PhaseStarted, trial="T001", phase="train")
        led.append(TrialFinished, trial="T001", status="failed_idea", reason="job died")
        assert build(led).budget_consumed_trials == 1
        # Analysis reads the logs: it was a preemption, not the idea's fault.
        led.append(TrialReclassified, trial="T001", status="errored", reason="preempted")
        state = build(led)
    assert state.budget_consumed_trials == 0
    assert state.trials["T001"].status == "errored"


def test_retry_replaces_phase_attempt(tmp_path):
    with Ledger(tmp_path / "events.jsonl") as led:
        led.append(CampaignStarted, campaign="c1")
        led.append(TrialCreated, trial="T001")
        led.append(PhaseStarted, trial="T001", phase="train")
        led.append(PhaseErrored, trial="T001", phase="train", error="flake")
        led.append(PhaseStarted, trial="T001", phase="train", attempt=2)
        state = build(led)
    phase = state.trials["T001"].phases["train"]
    assert phase.attempt == 2
    assert phase.status == "running"
    assert phase.job_id is None  # fresh attempt


def test_metric_correction_overrides(tmp_path):
    with Ledger(tmp_path / "events.jsonl") as led:
        led.append(CampaignStarted, campaign="c1")
        led.append(TrialCreated, trial="T001")
        led.append(PhaseStarted, trial="T001", phase="eval")
        led.append(
            PhaseCompleted, trial="T001", phase="eval", status="passed",
            metrics={"acc": 0.5}, verified=True,
        )
        led.append(
            MetricRecorded, trial="T001", metric="acc", value=0.7,
            phase="eval", verified=True,
        )
        state = build(led)
    assert state.trials["T001"].metrics["acc"].value == 0.7


def test_campaign_finish(tmp_path):
    with Ledger(tmp_path / "events.jsonl") as led:
        led.append(CampaignStarted, campaign="c1")
        led.append(CampaignFinished, reason="budget_reached")
        state = build(led)
    assert state.status == "finished"
    assert state.finish_reason == "budget_reached"


def test_idea_cannot_be_consumed_twice(tmp_path):
    with Ledger(tmp_path / "events.jsonl") as led:
        led.append(CampaignStarted, campaign="c1")
        led.append(IdeaCreated, idea="I1", source="human")
        led.append(TrialCreated, trial="T001", idea="I1")
        led.append(TrialCreated, trial="T002", idea="I1")
        with pytest.raises(StateError):
            build(led)


def test_unknown_trial_reference_raises(tmp_path):
    with Ledger(tmp_path / "events.jsonl") as led:
        led.append(CampaignStarted, campaign="c1")
        led.append(PhaseStarted, trial="TX", phase="train")
        with pytest.raises(StateError):
            build(led)
