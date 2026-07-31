"""CLI. Doc 09.

`run start` is deliberately separate from `campaign start`: a campaign is a
durable object that exists whether or not anything drives it; a run is a
process that drives it. That is what makes "the box died, start another one"
a normal operation rather than a recovery procedure.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import yaml

from .control.engine import Engine
from .store import transitions as tr
from .store.db import connect, new_id, tx
from .workflow import spec as wfspec

ARTIFACT_ROOT = os.environ.get("AUTORESEARCH_ARTIFACTS", "/tmp/autoresearch-artifacts")


def cmd_project_create(conn, args):
    pid = new_id()
    with tx(conn) as cur:
        cur.execute(
            """INSERT INTO project (project_id, name, description, metric_registry, created_by)
               VALUES (%s,%s,%s,%s,%s)""",
            (pid, args.name, args.description or "",
             json.dumps(yaml.safe_load(open(args.metrics)) if args.metrics else {}),
             os.environ.get("USER", "unknown")),
        )
    print(pid)


def cmd_campaign_create(conn, args):
    config = yaml.safe_load(open(args.config))
    wf = wfspec.build(config["workflow"])          # lint before anything is stored
    print(f"workflow ok: {len(wf.stages)} stages, order {wf.order}, "
          f"recovery tier '{wf.recovery_tier()}'", file=sys.stderr)

    cid = new_id()
    with tx(conn) as cur:
        cur.execute(
            """INSERT INTO campaign (campaign_id, project_id, config, config_hash, created_by)
               VALUES (%s,%s,%s,%s,%s)""",
            (cid, args.project, json.dumps(config), wf.workflow_version,
             os.environ.get("USER", "unknown")),
        )
        tr.log(cur, cid, "campaign", cid, "campaign_created",
               {"workflow": wf.name, "recovery_tier": wf.recovery_tier()}, to_state="DRAFT")
    print(cid)


def cmd_campaign_start(conn, args):
    with tx(conn) as cur:
        ok = tr.transition(cur, args.campaign, "campaign", args.campaign,
                           "DRAFT", "ACTIVE", "started", actor=os.environ.get("USER"))
    print("started" if ok else "not in DRAFT")


def cmd_idea_add(conn, args):
    """Seed hypotheses from a file. Stands in for the proposer, which the
    prototype deliberately omits — the durability story is testable without it."""
    ideas = yaml.safe_load(open(args.file))
    added = 0
    with tx(conn) as cur:
        for i, idea in enumerate(ideas):
            hid = new_id()
            cur.execute(
                """INSERT INTO hypothesis
                     (hypothesis_id, campaign_id, origin, actor, statement, rationale,
                      change_spec, structural_family, parameters, dedup_fingerprint,
                      priority, state, proposed_at_experiment_count)
                   VALUES (%s,%s,'human',%s,%s,%s,%s,%s,%s,%s,%s,'PROPOSED',0)
                   ON CONFLICT (campaign_id, dedup_fingerprint) DO NOTHING""",
                (hid, args.campaign, os.environ.get("USER", "unknown"),
                 idea["statement"], idea.get("rationale", ""),
                 json.dumps(idea.get("change_spec", {})),
                 idea.get("structural_family", "unspecified"),
                 json.dumps(idea.get("parameters", {})),
                 idea.get("fingerprint", f"seed-{i}-{idea['statement'][:40]}"),
                 idea.get("priority", 0)),
            )
            if cur.rowcount:
                tr.transition(cur, args.campaign, "hypothesis", hid, "PROPOSED", "QUEUED",
                              "admitted", actor=os.environ.get("USER"))
                added += 1
    print(f"queued {added} hypotheses")


def cmd_run_start(conn, args):
    engine = Engine(conn, args.campaign, ARTIFACT_ROOT, tick_seconds=args.tick)
    if not engine.start():
        sys.exit(3)
    engine.recover()
    engine.run_forever(max_ticks=args.max_ticks)


def cmd_status(conn, args):
    with tx(conn) as cur:
        cur.execute("SELECT * FROM campaign WHERE campaign_id = %s", (args.campaign,))
        c = cur.fetchone()
        cur.execute(
            """SELECT state, outcome, count(*) AS n FROM experiment
                WHERE campaign_id = %s GROUP BY state, outcome ORDER BY state""",
            (args.campaign,))
        exps = cur.fetchall()
        cur.execute(
            "SELECT state, count(*) AS n FROM hypothesis WHERE campaign_id = %s GROUP BY state",
            (args.campaign,))
        hyps = cur.fetchall()
        cur.execute("SELECT * FROM campaign_lease WHERE campaign_id = %s", (args.campaign,))
        lease = cur.fetchone()

    print(f"campaign {args.campaign}  {c['status']}"
          + (f"  ({c['stop_reason']})" if c["stop_reason"] else ""))
    if lease:
        print(f"  lease: run {lease['run_id'][:8]} token {lease['fencing_token']} "
              f"expires {lease['expires_at']:%H:%M:%S}")
    print("  hypotheses: " + ", ".join(f"{h['state']} {h['n']}" for h in hyps))
    for e in exps:
        print(f"  experiments: {e['state']:<12} {e['outcome'] or '':<20} {e['n']}")


def cmd_exp_list(conn, args):
    with tx(conn) as cur:
        cur.execute(
            """SELECT e.experiment_id, e.state, e.outcome, e.current_stage, e.metrics,
                      h.statement
                 FROM experiment e JOIN hypothesis h USING (hypothesis_id)
                WHERE e.campaign_id = %s ORDER BY e.created_at""",
            (args.campaign,))
        for r in cur.fetchall():
            metrics = ""
            if r["metrics"]:
                metrics = "  " + " ".join(
                    f"{k}={v['value']:.4g}" for k, v in r["metrics"].items())
            print(f"{r['experiment_id'][:8]}  {r['state']:<11} {r['outcome'] or '-':<20}"
                  f"{metrics}   {r['statement'][:50]}")


def cmd_history(conn, args):
    with tx(conn) as cur:
        cur.execute(
            """SELECT * FROM transition_log
                WHERE campaign_id = %s ORDER BY id DESC LIMIT %s""",
            (args.campaign, args.limit))
        rows = list(reversed(cur.fetchall()))
    for r in rows:
        arrow = f"{r['from_state'] or '':>13} -> {r['to_state'] or '':<13}" \
            if r["to_state"] else " " * 30
        detail = json.dumps(r["detail"])[:80] if r["detail"] not in ({}, None) else ""
        print(f"{r['occurred_at']:%H:%M:%S} {r['entity_type'][:6]:<6} "
              f"{r['entity_id'][:8]} {arrow} {r['reason']:<26} {detail}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="autoresearch")
    sub = p.add_subparsers(dest="cmd", required=True)

    x = sub.add_parser("project-create"); x.set_defaults(fn=cmd_project_create)
    x.add_argument("--name", required=True)
    x.add_argument("--description"); x.add_argument("--metrics")

    x = sub.add_parser("campaign-create"); x.set_defaults(fn=cmd_campaign_create)
    x.add_argument("--project", required=True); x.add_argument("--config", required=True)

    x = sub.add_parser("campaign-start"); x.set_defaults(fn=cmd_campaign_start)
    x.add_argument("campaign")

    x = sub.add_parser("idea-add"); x.set_defaults(fn=cmd_idea_add)
    x.add_argument("--campaign", required=True); x.add_argument("--file", required=True)

    x = sub.add_parser("run-start"); x.set_defaults(fn=cmd_run_start)
    x.add_argument("--campaign", required=True)
    x.add_argument("--tick", type=float, default=2.0)
    x.add_argument("--max-ticks", type=int, default=None, dest="max_ticks")

    x = sub.add_parser("status"); x.set_defaults(fn=cmd_status); x.add_argument("campaign")
    x = sub.add_parser("exp-list"); x.set_defaults(fn=cmd_exp_list); x.add_argument("campaign")
    x = sub.add_parser("history"); x.set_defaults(fn=cmd_history)
    x.add_argument("campaign"); x.add_argument("--limit", type=int, default=40)

    args = p.parse_args(argv)
    conn = connect()
    try:
        args.fn(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
