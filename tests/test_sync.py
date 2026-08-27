"""Mirroring to the durability tier, and recovering from it."""

import asyncio
import json

import pytest

from autoresearch import events as ev
from autoresearch.campaign import Campaign
from autoresearch.ledger import Ledger
from autoresearch.state import CampaignState
from autoresearch.sync import CampaignSync, DirectoryMirror, restore

CONFIG = """
name: t
goal: g
project_dir: {project}
workflow:
  train: {{uses: job, params: {{polls: 0, scale: 1.0}}}}
"""


def make_campaign(tmp_path, toy_project, mirror=None, interval=30.0):
    cfg = tmp_path / "campaign.yaml"
    cfg.write_text(CONFIG.format(project=toy_project))
    campaign_dir = tmp_path / "campaign"
    sync = (
        CampaignSync(campaign_dir, mirror, interval_s=interval)
        if mirror is not None
        else None
    )
    return Campaign(campaign_dir, cfg, sync=sync), sync


# -- incremental mirroring ---------------------------------------------------


def test_log_is_mirrored_incrementally(tmp_path, toy_project):
    mirror = DirectoryMirror(tmp_path / "remote")
    campaign, sync = make_campaign(tmp_path, toy_project, mirror)
    with campaign:
        campaign.create_trial("T001")
        first = sync.sync_log()
        assert first > 0

        campaign.create_trial("T002")
        second = sync.sync_log()
        assert second > 0
        assert second < first + 1000  # only the new bytes

        # A pass with nothing new copies nothing.
        assert sync.sync_log() == 0

    mirrored = (tmp_path / "remote" / "ledger" / "events.jsonl").read_bytes()
    local = (tmp_path / "campaign" / "ledger" / "events.jsonl").read_bytes()
    assert mirrored == local


def test_changed_files_mirrored_once(tmp_path, toy_project):
    mirror = DirectoryMirror(tmp_path / "remote")
    campaign, sync = make_campaign(tmp_path, toy_project, mirror)
    with campaign:
        campaign.create_trial("T001")
        assert sync.sync_files() > 0
        assert sync.sync_files() == 0  # unchanged files are not recopied

        campaign.recorder.record(ev.PhaseStarted, trial="T001", phase="train")
        assert sync.sync_files() > 0  # the view changed, so it is remirrored


def test_mirror_failure_does_not_stop_research(tmp_path, toy_project):
    class BrokenMirror(DirectoryMirror):
        def append(self, *a, **k):
            raise OSError("remote filesystem is down")

    campaign, sync = make_campaign(
        tmp_path, toy_project, BrokenMirror(tmp_path / "remote")
    )
    with campaign:
        campaign.create_trial("T001")
        sync.sync_once()  # must not raise
        assert sync.stats.failures == 1
        assert "down" in sync.stats.last_error
        # The campaign is unaffected and keeps recording.
        campaign.create_trial("T002")
        assert len(campaign.state.trials) == 2


# -- immediate push on job launch --------------------------------------------


def test_job_launch_is_mirrored_immediately(tmp_path, toy_project):
    """The one event that cannot wait for the next pass (§6.2)."""
    mirror = DirectoryMirror(tmp_path / "remote")
    campaign, sync = make_campaign(tmp_path, toy_project, mirror, interval=3600)
    with campaign:
        campaign.create_trial("T001")
        campaign.recorder.record(ev.PhaseStarted, trial="T001", phase="train")
        campaign.recorder.record(
            ev.JobLaunched, trial="T001", phase="train", job_id="xm-9", tag="t/T001/train"
        )
        # Without any periodic pass having run, the mirror already has it.
        mirrored = (tmp_path / "remote" / "ledger" / "events.jsonl").read_text()
    assert "xm-9" in mirrored


def test_ordinary_events_wait_for_the_next_pass(tmp_path, toy_project):
    mirror = DirectoryMirror(tmp_path / "remote")
    campaign, sync = make_campaign(tmp_path, toy_project, mirror, interval=3600)
    with campaign:
        campaign.create_trial("T001")
        remote_log = tmp_path / "remote" / "ledger" / "events.jsonl"
        assert not remote_log.exists() or "T001" not in remote_log.read_text()


# -- background task ---------------------------------------------------------


def test_background_task_syncs_and_flushes_on_stop(tmp_path, toy_project):
    mirror = DirectoryMirror(tmp_path / "remote")
    campaign, sync = make_campaign(tmp_path, toy_project, mirror, interval=0.01)

    async def scenario():
        sync.start()
        campaign.create_trial("T001")
        await asyncio.sleep(0.05)
        campaign.create_trial("T002")
        await sync.stop()  # final flush

    with campaign:
        asyncio.run(scenario())

    mirrored = (tmp_path / "remote" / "ledger" / "events.jsonl").read_text()
    assert "T001" in mirrored and "T002" in mirrored
    assert sync.stats.passes >= 2


# -- disaster recovery -------------------------------------------------------


def test_restore_from_mirror_resumes_the_campaign(tmp_path, toy_project):
    """Local disk lost: restore from the mirror and carry on, in-flight jobs
    still known."""
    mirror = DirectoryMirror(tmp_path / "remote")
    campaign, sync = make_campaign(tmp_path, toy_project, mirror)
    cfg = tmp_path / "campaign.yaml"
    with campaign:
        campaign.create_trial("T001")
        campaign.recorder.record(ev.PhaseStarted, trial="T001", phase="train")
        campaign.recorder.record(
            ev.JobLaunched, trial="T001", phase="train", job_id="xm-42", tag="t"
        )
        sync.sync_once()

    # The local disk goes away entirely.
    import shutil

    shutil.rmtree(tmp_path / "campaign")

    restored = restore(tmp_path / "remote", tmp_path / "recovered")
    with Campaign(restored, cfg) as c:
        assert c.state.trials["T001"].in_flight
        assert c.report.reattached_jobs == ["xm-42"]  # the job was not lost


def test_restore_refuses_to_overwrite(tmp_path, toy_project):
    mirror = DirectoryMirror(tmp_path / "remote")
    campaign, sync = make_campaign(tmp_path, toy_project, mirror)
    with campaign:
        sync.sync_once()
    with pytest.raises(FileExistsError):
        restore(tmp_path / "remote", tmp_path / "campaign")
