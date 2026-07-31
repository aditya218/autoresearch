"""Failure-injection suite. Doc 04 §6.

Durability claims that are not continuously tested are aspirations. These are
the minimum set: the controller is killed at the points where killing it is
most likely to lose or duplicate work.
"""
import os
import signal
import time

import psycopg2
import pytest

from autoresearch.domain import states
from autoresearch.store import transitions as tr
from autoresearch.store.db import new_id, tx
from conftest import make_campaign, query, spawn_run


def wait_for(fn, timeout=60, interval=0.4):
    deadline = time.time() + timeout
    while time.time() < deadline:
        val = fn()
        if val:
            return val
        time.sleep(interval)
    return None


# --------------------------------------------------------------------- basics

def test_campaign_runs_to_completion(conn, env):
    cid = make_campaign(conn, env, ideas=2, max_concurrent=2)
    proc = spawn_run(env, cid)
    try:
        done = wait_for(lambda: len(query(
            conn, "SELECT 1 FROM experiment WHERE campaign_id=%s AND state='SUCCEEDED'",
            (cid,))) == 2)
        assert done, "experiments did not complete"

        exps = query(conn, "SELECT * FROM experiment WHERE campaign_id=%s", (cid,))
        for e in exps:
            assert e["metrics"]["p50_latency_ms"]["value"] == pytest.approx(120.5)
            assert e["outcome"] == "success"
    finally:
        proc.kill()


# ------------------------------------------------------------ crash recovery

def test_kill_during_external_job_reattaches(conn, env):
    """SIGKILL while a job is running: the next run must adopt it, not relaunch.

    This is the case the whole re-attach design exists for. A relaunch here
    means two jobs burning compute, one of them unwatched.
    """
    cid = make_campaign(conn, env, ideas=1)
    proc = spawn_run(env, cid)

    launched = wait_for(lambda: query(
        conn, """SELECT * FROM stage_execution se JOIN replicate r USING (replicate_id)
                  JOIN experiment e USING (experiment_id)
                 WHERE e.campaign_id=%s AND se.stage_key='train'
                   AND se.state IN ('LAUNCHED','RUNNING')""", (cid,)))
    assert launched, "train never launched"
    original_job = launched[0]["job_id"]

    os.kill(proc.pid, signal.SIGKILL)
    proc.wait()

    with tx(conn) as cur:                       # the dead run's lease is stale
        cur.execute("UPDATE campaign_lease SET expires_at = now() - interval '1s'"
                    " WHERE campaign_id = %s", (cid,))

    proc2 = spawn_run(env, cid)
    try:
        assert wait_for(lambda: query(
            conn, "SELECT 1 FROM experiment WHERE campaign_id=%s AND state='SUCCEEDED'",
            (cid,))), "did not finish after restart"

        trains = query(conn, """SELECT se.* FROM stage_execution se
                                  JOIN replicate r USING (replicate_id)
                                  JOIN experiment e USING (experiment_id)
                                 WHERE e.campaign_id=%s AND se.stage_key='train'""", (cid,))
        assert len(trains) == 1, f"train relaunched: {len(trains)} attempts"
        assert trains[0]["job_id"] == original_job, "adopted a different job"

        # exactly one job ever submitted for this campaign
        spool = env["FAKEJOB_SPOOL"]
        jobs = os.listdir(spool) if os.path.isdir(spool) else []
        assert len(jobs) == 1, f"expected one submitted job, found {jobs}"
    finally:
        proc2.kill()


def test_kill_during_local_stage_reexecutes(conn, env):
    """A local stage is cheap by lint rule, so a crash mid-stage just redoes it."""
    cid = make_campaign(conn, env, ideas=1)
    proc = spawn_run(env, cid)
    wait_for(lambda: query(
        conn, """SELECT 1 FROM stage_execution se JOIN replicate r USING (replicate_id)
                  JOIN experiment e USING (experiment_id)
                 WHERE e.campaign_id=%s AND se.stage_key='prepare'""", (cid,)), timeout=30)
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait()
    with tx(conn) as cur:
        cur.execute("UPDATE campaign_lease SET expires_at = now() - interval '1s'"
                    " WHERE campaign_id = %s", (cid,))

    proc2 = spawn_run(env, cid)
    try:
        assert wait_for(lambda: query(
            conn, "SELECT 1 FROM experiment WHERE campaign_id=%s AND state='SUCCEEDED'",
            (cid,))), "did not recover"
    finally:
        proc2.kill()


def test_orphaned_launch_intent_is_adopted(conn, env):
    """Simulate a crash inside the launch window itself.

    A stage row sits in LAUNCH_INTENT while a job is genuinely running and its
    id was never committed. Recovery must find it via the receipt rather than
    submitting a second job.
    """
    import subprocess
    from autoresearch.executors.base import idem_key, receipt_path

    cid = make_campaign(conn, env, ideas=1)
    proc = spawn_run(env, cid)
    launched = wait_for(lambda: query(
        conn, """SELECT se.*, r.experiment_id FROM stage_execution se
                   JOIN replicate r USING (replicate_id)
                   JOIN experiment e USING (experiment_id)
                  WHERE e.campaign_id=%s AND se.stage_key='train'
                    AND se.state IN ('LAUNCHED','RUNNING')""", (cid,)))
    assert launched
    se = launched[0]
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait()

    # Rewind the row to the ambiguous state: job running, id not recorded.
    with tx(conn) as cur:
        cur.execute("""UPDATE stage_execution SET state='LAUNCH_INTENT', job_id=NULL
                        WHERE stage_execution_id=%s""", (se["stage_execution_id"],))
        cur.execute("UPDATE campaign_lease SET expires_at = now() - interval '1s'"
                    " WHERE campaign_id = %s", (cid,))

    proc2 = spawn_run(env, cid)
    try:
        recovered = wait_for(lambda: [
            r for r in query(conn, "SELECT * FROM stage_execution WHERE stage_execution_id=%s",
                             (se["stage_execution_id"],))
            if r["state"] in ("LAUNCHED", "RUNNING", "COMPLETED")])
        assert recovered, "LAUNCH_INTENT was never resolved"
        assert recovered[0]["job_id"] == se["job_id"], "did not adopt the original job"

        jobs = os.listdir(env["FAKEJOB_SPOOL"])
        assert len(jobs) == 1, f"a second job was submitted: {jobs}"
    finally:
        proc2.kill()


# ------------------------------------------------------------ lease + fencing

def test_second_run_is_refused_while_lease_is_live(conn, env):
    cid = make_campaign(conn, env, ideas=1)
    proc = spawn_run(env, cid)
    try:
        assert wait_for(lambda: query(
            conn, "SELECT 1 FROM campaign_lease WHERE campaign_id=%s", (cid,)))
        second = spawn_run(env, cid)
        assert second.wait(timeout=30) == 3, "second run should exit 3"
        assert "lease held" in (second.stdout.read() or "")
    finally:
        proc.kill()


def test_stale_fencing_token_is_rejected(conn, env):
    """A zombie run's writes must fail regardless of what the zombie believes."""
    cid = make_campaign(conn, env, ideas=1)
    run_id = new_id()
    from autoresearch.control import lease as lease_mod

    with tx(conn) as cur:
        cur.execute("INSERT INTO run (run_id, campaign_id, fencing_token, worker_identity,"
                    " engine_version) VALUES (%s,%s,1,'t','t')", (run_id, cid))
        token = lease_mod.acquire(cur, cid, run_id)
    assert token == 1

    with tx(conn) as cur:                       # someone seizes the lease
        cur.execute("UPDATE campaign_lease SET expires_at = now() - interval '1s'"
                    " WHERE campaign_id=%s", (cid,))
        new_token = lease_mod.acquire(cur, cid, new_id())
    assert new_token == 2

    with pytest.raises(states.StaleFence):
        with tx(conn) as cur:
            tr.transition(cur, cid, "campaign", cid, "ACTIVE", "PAUSED",
                          "zombie_write", run_id=run_id, fencing_token=token)


# ------------------------------------------------------------------ integrity

def test_illegal_transition_is_rejected(conn, env):
    cid = make_campaign(conn, env, ideas=0)
    with pytest.raises(states.IllegalTransition):
        with tx(conn) as cur:
            tr.transition(cur, cid, "campaign", cid, "ACTIVE", "ARCHIVED", "nope")


def test_cas_loser_does_not_clobber(conn, env):
    """Two writers, one entity: exactly one wins, the loser is told so."""
    cid = make_campaign(conn, env, ideas=0)
    with tx(conn) as cur:
        assert tr.transition(cur, cid, "campaign", cid, "ACTIVE", "PAUSED", "first")
    with tx(conn) as cur:
        # second writer still believes the campaign is ACTIVE
        assert tr.transition(cur, cid, "campaign", cid, "ACTIVE", "PAUSED", "second") is False


def test_transition_log_is_append_only(conn, env):
    cid = make_campaign(conn, env, ideas=0)
    with tx(conn) as cur:
        tr.log(cur, cid, "campaign", cid, "marker")
    with pytest.raises(psycopg2.errors.RaiseException):
        with tx(conn) as cur:
            cur.execute("UPDATE transition_log SET reason='tampered' WHERE campaign_id=%s", (cid,))
    with pytest.raises(psycopg2.errors.RaiseException):
        with tx(conn) as cur:
            cur.execute("DELETE FROM transition_log WHERE campaign_id=%s", (cid,))


def test_terminal_experiment_is_frozen(conn, env):
    cid = make_campaign(conn, env, ideas=1)
    proc = spawn_run(env, cid)
    try:
        assert wait_for(lambda: query(
            conn, "SELECT * FROM experiment WHERE campaign_id=%s AND state='SUCCEEDED'", (cid,)))
        exp = query(conn, "SELECT * FROM experiment WHERE campaign_id=%s", (cid,))[0]
        with pytest.raises(psycopg2.errors.RaiseException):
            with tx(conn) as cur:
                cur.execute("UPDATE experiment SET metrics='{}' WHERE experiment_id=%s",
                            (exp["experiment_id"],))
        # retraction is allowed, and does not erase the result
        with tx(conn) as cur:
            cur.execute("""UPDATE experiment SET invalidated_at=now(),
                             invalidation_reason='contamination' WHERE experiment_id=%s""",
                        (exp["experiment_id"],))
        after = query(conn, "SELECT * FROM experiment WHERE experiment_id=%s",
                      (exp["experiment_id"],))[0]
        assert after["metrics"] == exp["metrics"]
    finally:
        proc.kill()


def test_state_change_and_log_row_commit_together(conn, env):
    """The rule that makes the debug log trustworthy: same transaction, always."""
    cid = make_campaign(conn, env, ideas=0)
    before = len(query(conn, "SELECT 1 FROM transition_log WHERE campaign_id=%s", (cid,)))
    try:
        with tx(conn) as cur:
            tr.transition(cur, cid, "campaign", cid, "ACTIVE", "PAUSED", "will_roll_back")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    after = query(conn, "SELECT status FROM campaign WHERE campaign_id=%s", (cid,))[0]
    log_n = len(query(conn, "SELECT 1 FROM transition_log WHERE campaign_id=%s", (cid,)))
    assert after["status"] == "ACTIVE", "state change survived a rollback"
    assert log_n == before, "log row survived a rollback"
