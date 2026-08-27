import pytest
from pydantic import ValidationError

from autoresearch.events import (
    JobLaunched,
    MetricRecorded,
    dump_event,
    parse_event,
    utc_now,
)


def test_round_trip():
    e = MetricRecorded(
        seq=481, ts=utc_now(), trial="T012", metric="accuracy", value=0.91
    )
    parsed = parse_event(dump_event(e))
    assert parsed == e
    assert parsed.type == "metric_recorded"


def test_discriminated_by_type():
    e = JobLaunched(seq=1, ts=utc_now(), trial="T001", phase="train", job_id="j-9")
    parsed = parse_event(dump_event(e))
    assert isinstance(parsed, JobLaunched)
    assert parsed.job_id == "j-9"


def test_unknown_type_rejected():
    with pytest.raises(ValidationError):
        parse_event('{"seq": 1, "ts": "2026-08-26T00:00:00Z", "type": "nonsense"}')


def test_extra_fields_rejected():
    with pytest.raises(ValidationError):
        MetricRecorded(
            seq=1, ts=utc_now(), trial="T1", metric="m", value=1.0, bogus=True
        )


def test_seq_must_be_positive():
    with pytest.raises(ValidationError):
        MetricRecorded(seq=0, ts=utc_now(), trial="T1", metric="m", value=1.0)
