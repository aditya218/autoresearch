import json
import os
import subprocess
import sys
import uuid

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from autoresearch.store.db import connect, new_id, tx          # noqa: E402
from autoresearch.store import transitions as tr               # noqa: E402
from autoresearch.workflow import spec as wfspec               # noqa: E402

FAKEJOB = os.path.join(ROOT, "examples", "fakejob")


@pytest.fixture
def conn():
    c = connect()
    yield c
    c.close()


@pytest.fixture
def env(tmp_path):
    """A private spool and artifact root per test, so tests cannot collide."""
    e = {
        **os.environ,
        "PYTHONPATH": ROOT,
        "FAKEJOB_DIR": FAKEJOB,
        "FAKEJOB_SPOOL": str(tmp_path / "spool"),
        "FAKEJOB_DURATION": "6",
        "AUTORESEARCH_ARTIFACTS": str(tmp_path / "artifacts"),
    }
    return e


def make_campaign(conn, env, ideas=1, max_concurrent=1, duration="6", stages=None):
    project_id, campaign_id = new_id(), new_id()
    workflow = stages or {
        "name": "test-wf",
        "version": 1,
        "stages": [
            {"key": "prepare", "kind": "local", "timeout": "2m",
             "command": ["bash", "-c", "echo prep"]},
            {"key": "train", "kind": "external_job", "needs": ["prepare"],
             "launch": f"{FAKEJOB}/launch.sh",
             "poll": f"{FAKEJOB}/status.sh {{{{ job_id }}}}",
             "find": f"{FAKEJOB}/find.sh",
             "timeout": "30m", "poll_interval": "1s",
             "status_map": {"PREEMPTED": "infra"},
             "outputs": {"metrics": "{{ artifact_dir }}/metrics.json"}},
        ],
        "terminal": ["train"],
    }
    config = {
        "budget": {"max_experiments": 10, "max_concurrent_experiments": max_concurrent},
        "provenance_pins": {"base_commit": "test"},
        "workflow": workflow,
    }
    wf = wfspec.build(workflow)
    with tx(conn) as cur:
        cur.execute(
            "INSERT INTO project (project_id, name, metric_registry, created_by)"
            " VALUES (%s,%s,'{}','test')", (project_id, f"p-{project_id[:8]}"))
        cur.execute(
            """INSERT INTO campaign (campaign_id, project_id, config, config_hash,
                                     status, created_by)
               VALUES (%s,%s,%s,%s,'ACTIVE','test')""",
            (campaign_id, project_id, json.dumps(config), wf.workflow_version))
        for i in range(ideas):
            hid = new_id()
            cur.execute(
                """INSERT INTO hypothesis (hypothesis_id, campaign_id, origin, statement,
                     rationale, change_spec, structural_family, parameters,
                     dedup_fingerprint, state, proposed_at_experiment_count)
                   VALUES (%s,%s,'seed',%s,'','{}','fam','{}',%s,'QUEUED',0)""",
                (hid, campaign_id, f"idea {i}", f"fp-{i}"))
    return campaign_id


def spawn_run(env, campaign_id, tick="0.5"):
    return subprocess.Popen(
        [sys.executable, "-m", "autoresearch.cli", "run-start",
         "--campaign", campaign_id, "--tick", tick],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


def query(conn, sql, params=()):
    with tx(conn) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]
