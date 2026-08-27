import json

from autoresearch.events import (
    CampaignStarted,
    IdeaCreated,
    PhaseCompleted,
    PhaseStarted,
    TrialCreated,
    TrialFinished,
)
from autoresearch.ledger import Ledger
from autoresearch.state import CampaignState
from autoresearch.views import ViewWriter


def make_state(tmp_path) -> CampaignState:
    with Ledger(tmp_path / "ledger" / "events.jsonl") as led:
        led.append(CampaignStarted, campaign="c1")
        led.append(IdeaCreated, idea="I1", source="ideator")
        led.append(TrialCreated, trial="T001", idea="I1")
        led.append(PhaseStarted, trial="T001", phase="eval")
        led.append(
            PhaseCompleted, trial="T001", phase="eval", status="passed",
            metrics={"acc": 0.9}, verified=True,
        )
        led.append(TrialFinished, trial="T001", status="completed")
        led.append(IdeaCreated, idea="I2", source="human")
        return CampaignState.replay(led.events())


def test_trial_and_index_views_written(tmp_path):
    state = make_state(tmp_path)
    writer = ViewWriter(tmp_path)
    writer.write_trial(state.trials["T001"])
    writer.write_index(state)

    trial = json.loads(writer.trial_path("T001").read_text())
    assert trial["status"] == "completed"
    assert trial["metrics"]["acc"] == {"value": 0.9, "verified": True, "phase": "eval"}
    assert trial["materialized_seq"] == state.trials["T001"].updated_seq

    index = json.loads(writer.index_path.read_text())
    assert index["campaign"] == "c1"
    assert index["backlog"] == ["I2"]
    assert [row["trial"] for row in index["trials"]] == ["T001"]
    assert index["materialized_seq"] == state.last_seq


def test_no_tmp_files_left(tmp_path):
    state = make_state(tmp_path)
    writer = ViewWriter(tmp_path)
    writer.write_trial(state.trials["T001"])
    writer.write_index(state)
    assert not list(tmp_path.rglob("*.tmp"))


def test_regenerate_stale(tmp_path):
    state = make_state(tmp_path)
    writer = ViewWriter(tmp_path)

    # Nothing materialized yet: everything is stale.
    rewritten = writer.regenerate_stale(state)
    assert set(rewritten) == {"T001", "index"}

    # Everything current: nothing rewritten.
    assert writer.regenerate_stale(state) == []

    # A corrupt (hand-mangled) view is treated as stale and healed.
    writer.trial_path("T001").write_text("{ not json")
    assert writer.regenerate_stale(state) == ["T001"]
    assert json.loads(writer.trial_path("T001").read_text())["status"] == "completed"
