import pytest

from autoresearch.events import CampaignStarted, MetricRecorded, TrialCreated
from autoresearch.ledger import Ledger, LedgerError


def test_append_assigns_contiguous_seq(tmp_path):
    with Ledger(tmp_path / "events.jsonl") as led:
        e1 = led.append(CampaignStarted, campaign="c1")
        e2 = led.append(TrialCreated, trial="T001")
        assert (e1.seq, e2.seq) == (1, 2)
        assert led.last_seq == 2


def test_reopen_continues_seq(tmp_path):
    path = tmp_path / "events.jsonl"
    with Ledger(path) as led:
        led.append(CampaignStarted, campaign="c1")
    with Ledger(path) as led:
        assert led.last_seq == 1
        e = led.append(TrialCreated, trial="T001")
        assert e.seq == 2


def test_events_round_trip(tmp_path):
    path = tmp_path / "events.jsonl"
    with Ledger(path) as led:
        led.append(CampaignStarted, campaign="c1")
        led.append(TrialCreated, trial="T001")
        led.append(MetricRecorded, trial="T001", metric="acc", value=0.5)
        events = list(led.events())
    assert [e.type for e in events] == [
        "campaign_started",
        "trial_created",
        "metric_recorded",
    ]
    assert [e.seq for e in events] == [1, 2, 3]


def test_torn_final_line_truncated(tmp_path):
    path = tmp_path / "events.jsonl"
    with Ledger(path) as led:
        led.append(CampaignStarted, campaign="c1")
        led.append(TrialCreated, trial="T001")
    # Simulate a crash mid-write: garbage with no trailing newline.
    with open(path, "ab") as fh:
        fh.write(b'{"seq": 3, "ts": "2026-')
    with Ledger(path) as led:
        assert led.recovered_bytes > 0
        assert led.last_seq == 2
        e = led.append(TrialCreated, trial="T002")
        assert e.seq == 3
        assert [ev.seq for ev in led.events()] == [1, 2, 3]


def test_terminated_garbage_line_is_corruption(tmp_path):
    path = tmp_path / "events.jsonl"
    with Ledger(path) as led:
        led.append(CampaignStarted, campaign="c1")
    with open(path, "ab") as fh:
        fh.write(b"this is not json\n")
    with pytest.raises(LedgerError):
        Ledger(path)


def test_sequence_gap_is_corruption(tmp_path):
    path = tmp_path / "events.jsonl"
    with Ledger(path) as led:
        e1 = led.append(CampaignStarted, campaign="c1")
    # Hand-craft a line that skips seq 2.
    line = e1.model_copy(update={"seq": 3}).model_dump_json() + "\n"
    with open(path, "ab") as fh:
        fh.write(line.encode())
    with pytest.raises(LedgerError):
        Ledger(path)


def test_empty_file_is_clean(tmp_path):
    path = tmp_path / "events.jsonl"
    path.touch()
    with Ledger(path) as led:
        assert led.last_seq == 0
        assert led.recovered_bytes == 0
