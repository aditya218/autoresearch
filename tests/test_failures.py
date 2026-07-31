"""Failure taxonomy and workflow lint.

The infra/experiment distinction decides both whether to retry and what a
result means. Collapsing them is the mistake that makes an autonomous loop
draw wrong conclusions and burn budget on doomed retries.
"""
import os

import pytest

from autoresearch.workflow import spec as wfspec
from conftest import FAKEJOB, make_campaign, query, spawn_run
from test_durability import wait_for


def workflow_with(fail_status: str):
    return {
        "name": "fail-wf", "version": 1,
        "stages": [
            {"key": "train", "kind": "external_job",
             "launch": f"{FAKEJOB}/launch.sh",
             "poll": f"{FAKEJOB}/status.sh {{{{ job_id }}}}",
             "find": f"{FAKEJOB}/find.sh",
             "logs": f"{FAKEJOB}/logs.sh {{{{ job_id }}}}",
             "timeout": "30m", "poll_interval": "1s", "max_infra_retries": 2,
             "status_map": {"PREEMPTED": "infra", "FAILED": "experiment"},
             "outputs": {"metrics": "{{ artifact_dir }}/metrics.json"}},
        ],
        "terminal": ["train"],
    }


def test_infra_failure_retries_then_gives_up(conn, env):
    """A preempted node is not a research result: retry, up to the ceiling."""
    env = {**env, "FAKEJOB_FAIL": "PREEMPTED", "FAKEJOB_DURATION": "1"}
    cid = make_campaign(conn, env, ideas=1, stages=workflow_with("PREEMPTED"))
    proc = spawn_run(env, cid)
    try:
        assert wait_for(lambda: query(
            conn, "SELECT 1 FROM experiment WHERE campaign_id=%s AND state='FAILED'",
            (cid,)), timeout=90), "experiment never resolved"

        attempts = query(conn, """SELECT se.* FROM stage_execution se
                                    JOIN replicate r USING (replicate_id)
                                    JOIN experiment e USING (experiment_id)
                                   WHERE e.campaign_id=%s ORDER BY se.attempt""", (cid,))
        assert len(attempts) == 3, f"expected 1 try + 2 retries, got {len(attempts)}"
        assert all(a["failure_class"] == "infra" for a in attempts)

        exp = query(conn, "SELECT * FROM experiment WHERE campaign_id=%s", (cid,))[0]
        assert exp["outcome"] == "infra_failure", "infra must not become a research result"
    finally:
        proc.kill()


def test_experiment_failure_does_not_retry(conn, env):
    """A change that genuinely broke is a finding. Retrying it burns budget for
    no information, and the same failure would recur identically."""
    env = {**env, "FAKEJOB_FAIL": "FAILED", "FAKEJOB_DURATION": "1"}
    cid = make_campaign(conn, env, ideas=1, stages=workflow_with("FAILED"))
    proc = spawn_run(env, cid)
    try:
        assert wait_for(lambda: query(
            conn, "SELECT 1 FROM experiment WHERE campaign_id=%s AND state='FAILED'",
            (cid,)), timeout=90)
        attempts = query(conn, """SELECT se.* FROM stage_execution se
                                    JOIN replicate r USING (replicate_id)
                                    JOIN experiment e USING (experiment_id)
                                   WHERE e.campaign_id=%s""", (cid,))
        assert len(attempts) == 1, f"experiment failure was retried {len(attempts)} times"
        exp = query(conn, "SELECT * FROM experiment WHERE campaign_id=%s", (cid,))[0]
        assert exp["outcome"] == "experiment_failure"
    finally:
        proc.kill()


# ---------------------------------------------------------------- lint rules

def base_stage(**kw):
    st = {"key": "train", "kind": "external_job", "launch": "l.sh", "poll": "p.sh",
          "outputs": {"metrics": "m.json"}}
    st.update(kw)
    return st


def test_local_stage_ceiling_is_enforced():
    """The lint rule that makes the durability guarantee real: a crash
    re-executes a local stage, so expensive work may not hide in one."""
    with pytest.raises(wfspec.SpecError, match="exceeds ceiling"):
        wfspec.build({"name": "w", "stages": [
            base_stage(),
            {"key": "slow", "kind": "local", "timeout": "45m",
             "command": ["true"]}]})


def test_implement_gets_the_raised_ceiling():
    wf = wfspec.build({"name": "w", "stages": [
        base_stage(),
        {"key": "implement", "kind": "local", "timeout": "45m", "command": ["true"]}]})
    assert wf.stages["implement"].timeout == 45 * 60


def test_cycles_are_rejected():
    with pytest.raises(wfspec.SpecError, match="cycle"):
        wfspec.build({"name": "w", "stages": [
            {"key": "a", "kind": "local", "command": ["true"], "needs": ["b"]},
            {"key": "b", "kind": "local", "command": ["true"], "needs": ["a"]},
            base_stage()]})


def test_external_job_needs_launch_and_poll():
    with pytest.raises(wfspec.SpecError, match="needs launch and poll"):
        wfspec.build({"name": "w", "stages": [
            {"key": "train", "kind": "external_job", "outputs": {"metrics": "m"}}]})


def test_find_is_optional_and_tier_is_reported():
    """D11: `find` is recommended, not required. The tier is recorded so a
    report can state honestly what the recovery guarantee was."""
    wf = wfspec.build({"name": "w", "stages": [base_stage()]})
    assert wf.recovery_tier() == "receipt"
    wf2 = wfspec.build({"name": "w", "stages": [base_stage(find="f.sh")]})
    assert wf2.recovery_tier() == "find"


def test_exactly_one_metrics_stage():
    with pytest.raises(wfspec.SpecError, match="outputs.metrics"):
        wfspec.build({"name": "w", "stages": [
            {"key": "a", "kind": "local", "command": ["true"]}]})
    with pytest.raises(wfspec.SpecError, match="outputs.metrics"):
        wfspec.build({"name": "w", "stages": [
            base_stage(), base_stage(key="train2")]})


def test_unknown_dependency_is_rejected():
    with pytest.raises(wfspec.SpecError, match="unknown dependency"):
        wfspec.build({"name": "w", "stages": [base_stage(needs=["ghost"])]})
