"""Engine CLI.

    autoresearch validate  <campaign.yaml>
    autoresearch run-phase <campaign.yaml> <phase> --workspace DIR --out DIR
    autoresearch run-one    <campaign.yaml> --campaign-dir DIR [--idea FILE]
    autoresearch status     <campaign-dir>

`run-phase` executes exactly one phase against a directory, with no campaign,
no ledger, and no trial. `run-one` runs a single hand-written idea through the
whole workflow with the full ledger - the debugging tool, the workflow-setup
validator, and the fully human-curated mode in one (§6).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

from autoresearch.campaign import Campaign
from autoresearch.config import CampaignConfig, ConfigError, load_config
from autoresearch.engine import run_trial
from autoresearch.phases import JobPhase, PhaseFailure, run_local_phase
from autoresearch.project import DONE, FAILED, KNOWN_STATUSES, Project


def _project_for(cfg: CampaignConfig, config_path: Path) -> Project:
    project_dir = Path(cfg.project_dir)
    if not project_dir.is_absolute():
        project_dir = (config_path.parent / project_dir).resolve()
    return Project(project_dir)


def cmd_validate(args: argparse.Namespace) -> int:
    path = Path(args.config)
    try:
        cfg = load_config(path)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    project = _project_for(cfg, path)
    problems: list[str] = []
    if not project.dir.exists():
        problems.append(f"project dir not found: {project.dir}")
    else:
        needed = set()
        for phase in cfg.workflow.values():
            if phase.uses == "job":
                needed |= {"launch", "poll", "collect"}
            elif phase.uses is not None:
                needed.add("run")
        for name in sorted(needed):
            script = project.script_path(name)
            if not script.exists():
                problems.append(f"missing project script: {script}")
            elif not script.stat().st_mode & 0o111:
                problems.append(f"project script not executable: {script}")

    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return 1

    order = cfg.phase_order()
    print(f"config ok: {cfg.name}")
    print(f"  phases:      {' -> '.join(order)}")
    gates = [n for n in order if cfg.workflow[n].gate]
    print(f"  gates:       {', '.join(gates) if gates else '(none)'}")
    for metric, mcfg in cfg.key_metrics.items():
        print(f"  key metric:  {metric} from {mcfg.from_phase} ({mcfg.goal})")
    print(
        f"  budget:      max_trials={cfg.budget.max_trials} "
        f"active_trials={cfg.budget.active_trials}"
    )
    return 0


def cmd_run_phase(args: argparse.Namespace) -> int:
    path = Path(args.config)
    try:
        cfg = load_config(path)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.phase not in cfg.workflow:
        print(f"error: no phase {args.phase!r} in workflow", file=sys.stderr)
        return 1

    phase_cfg = cfg.workflow[args.phase]
    if phase_cfg.agentic:
        print(
            f"error: {args.phase!r} is an agentic phase; agent execution "
            f"arrives with the harness adapter",
            file=sys.stderr,
        )
        return 2

    project = _project_for(cfg, path)
    workspace = Path(args.workspace).resolve()
    out_dir = Path(args.out).resolve()

    try:
        if phase_cfg.uses == "job":
            job = JobPhase(
                project=project, phase=args.phase, cfg=phase_cfg,
                workspace=workspace, phase_dir=out_dir,
                tag=args.tag or f"run-phase/{args.phase}",
            )
            job_id = job.launch()
            print(f"launched job {job_id}")
            while True:
                status = job.poll()
                print(f"  status: {status}")
                if status in (DONE, FAILED):
                    break
                if status not in KNOWN_STATUSES:
                    print(
                        f"error: unknown job status {status!r} "
                        f"(a repair-agent situation)",
                        file=sys.stderr,
                    )
                    return 3
                time.sleep(args.poll_interval)
            if job.status == FAILED:
                print("job failed")
                return 3
            outcome = job.collect()
        else:
            outcome = run_local_phase(
                project, args.phase, phase_cfg, workspace, out_dir
            )
    except PhaseFailure as exc:
        print(f"phase failure: {exc}", file=sys.stderr)
        return 3

    print(
        json.dumps(
            {
                "status": outcome.status,
                "metrics": outcome.metrics,
                "verified": outcome.verified,
                "notes": outcome.notes,
                "job_id": outcome.job_id,
            },
            indent=2,
        )
    )
    return 0 if outcome.status == "passed" else 1


def _scripted_agentic(idea_file: Path | None):
    """Stand-in for the agent harness: seeds the workspace from a hand-written
    idea file and reports a phase result, so `run-one` exercises the whole
    workflow before the harness adapter lands."""
    from autoresearch.contract import PhaseResult, write_result
    from autoresearch.phases import PhaseOutcome, finalize

    def run(phase: str, cfg, workspace: Path, phase_dir: Path) -> PhaseOutcome:
        if idea_file is not None and idea_file.exists():
            shutil.copyfile(idea_file, workspace / "change.json")
        elif not (workspace / "change.json").exists():
            (workspace / "change.json").write_text(
                json.dumps({"name": "baseline", "delta": 0.0}) + "\n"
            )
        for rel in cfg.produces:
            target = phase_dir / rel
            if not target.exists() and (workspace / rel).exists():
                shutil.copyfile(workspace / rel, target)
        write_result(
            phase_dir,
            PhaseResult(status="passed", notes=f"scripted stand-in for {phase}"),
        )
        return finalize(phase_dir, cfg, verified=False, workspace=workspace)

    return run


def cmd_run_one(args: argparse.Namespace) -> int:
    config_path = Path(args.config)
    try:
        campaign = Campaign(args.campaign_dir, config_path)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    with campaign:
        report = campaign.report
        if report.recovered_bytes:
            print(f"recovered {report.recovered_bytes} bytes from a torn write")
        if report.resumed_trials:
            print(f"resuming trials: {', '.join(report.resumed_trials)}")
            if report.reattached_jobs:
                print(f"reattached jobs: {', '.join(report.reattached_jobs)}")
            trial_id = report.resumed_trials[0]
        else:
            trial_id = campaign.next_trial_id()
            campaign.create_trial(trial_id, idea=None)
            campaign.prepare_workspace(trial_id, base_dir=args.base_dir)
            print(f"created {trial_id}")

        ctx = campaign.trial_context(
            trial_id,
            poll_interval=args.poll_interval,
            run_agentic=_scripted_agentic(Path(args.idea) if args.idea else None),
        )
        status = asyncio.run(run_trial(ctx))

        trial = campaign.state.trials[trial_id]
        print(f"{trial_id}: {status}" + (f" ({trial.reason})" if trial.reason else ""))
        for name, metric in trial.metrics.items():
            mark = "" if metric.verified else " (unverified)"
            print(f"  {name}: {metric.value}{mark}")
    return 0 if status == "completed" else 1


def _harness_from_args(args: argparse.Namespace):
    """Build the agent harness named on the command line, or None to use the
    scripted stand-in. Any harness CLI works: `--harness 'claude -p'`."""
    if not getattr(args, "harness", None):
        return None
    from autoresearch.agents import CommandHarness

    return CommandHarness(
        command=args.harness.split(), skill_arg=getattr(args, "skill_arg", None)
    )


def _durability_from_args(args: argparse.Namespace, campaign_dir: str):
    """Build the VCS adapter and remote mirror named on the command line."""
    vcs = None
    if getattr(args, "vcs", None):
        from autoresearch.vcs import make_vcs

        vcs = make_vcs(
            args.vcs, args.repo or ".", Path(campaign_dir) / "workspaces"
        )
    sync = None
    if getattr(args, "mirror", None):
        from autoresearch.sync import CampaignSync, DirectoryMirror

        sync = CampaignSync(
            campaign_dir,
            DirectoryMirror(args.mirror),
            interval_s=args.sync_interval,
        )
    return vcs, sync


def cmd_run(args: argparse.Namespace) -> int:
    """Run a campaign: baseline, then the two loops until budget or stall."""
    from autoresearch.agentic import make_agentic_runner
    from autoresearch.ideator import AgentIdeator
    from autoresearch.loop import CampaignLoop
    from autoresearch.vcs import VCSError

    config_path = Path(args.config)
    try:
        vcs, sync = _durability_from_args(args, args.campaign_dir)
        campaign = Campaign(args.campaign_dir, config_path, vcs=vcs, sync=sync)
    except (ConfigError, VCSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    with campaign:
        report = campaign.report
        if report.recovered_bytes:
            print(f"recovered {report.recovered_bytes} bytes from a torn write")
        if report.resumed_trials:
            print(f"resuming trials: {', '.join(report.resumed_trials)}")
        if report.reattached_jobs:
            print(f"reattached jobs: {', '.join(report.reattached_jobs)}")
        if campaign.state.status != "running":
            print(f"campaign was {campaign.state.status}; resuming")

        harness = _harness_from_args(args)
        if harness is None:
            run_agentic = _scripted_agentic(Path(args.idea) if args.idea else None)
            ideator = None
            print("no --harness given: using the scripted stand-in, no ideation")
        else:
            def run_agentic(phase, cfg, workspace, phase_dir):
                # Bind the runner to whichever trial is executing this phase.
                trial_id = workspace.name
                idea_file = workspace / "idea.json"
                idea = (
                    json.loads(idea_file.read_text()) if idea_file.exists() else None
                )
                runner = make_agentic_runner(
                    harness, campaign.config, campaign.state, trial_id, idea=idea
                )
                return runner(phase, cfg, workspace, phase_dir)

            ideator = AgentIdeator(harness) if args.ideate else None

        repair_agent = None
        if harness is not None and not args.no_repair:
            from autoresearch.repair import RepairAgent

            repair_agent = RepairAgent(harness)

        loop = CampaignLoop(
            campaign,
            ideator=ideator,
            run_agentic=run_agentic,
            repair_agent=repair_agent,
            poll_interval=args.poll_interval,
        )
        if campaign.state.status != "running":
            loop.resume()
        for _ in range(args.inject or 0):
            loop.inject_idea()

        async def drive() -> object:
            if sync is not None:
                sync.start()
            try:
                return await loop.run()
            finally:
                if sync is not None:
                    await sync.stop()  # final flush to the mirror

        result = asyncio.run(drive())
        print(f"campaign {result.status}")
        if sync is not None:
            print(
                f"mirrored: {sync.stats.log_bytes} log bytes, "
                f"{sync.stats.files} files, {sync.stats.failures} failed passes"
            )
        for trial in campaign.state.trials.values():
            metrics = " ".join(
                f"{n}={m.value}" + ("" if m.verified else "?")
                for n, m in trial.metrics.items()
            )
            print(f"  {trial.trial}: {trial.status}  {metrics}")
    return 0 if result.status in {"budget_reached", "stopped"} else 1


def cmd_status(args: argparse.Namespace) -> int:
    campaign_dir = Path(args.campaign_dir)
    index_path = campaign_dir / "index" / "trials.json"
    if not index_path.exists():
        print(f"error: no campaign index at {index_path}", file=sys.stderr)
        return 1
    index = json.loads(index_path.read_text())

    print(f"campaign: {index['campaign']}  [{index['status']}]")
    if index.get("finish_reason"):
        print(f"  finished: {index['finish_reason']}")
    if index.get("backlog"):
        print(f"  backlog: {', '.join(index['backlog'])}")
    print(f"  trials: {len(index['trials'])}")
    for row in index["trials"]:
        metrics = " ".join(
            f"{name}={m['value']}" + ("" if m["verified"] else "?")
            for name, m in row["metrics"].items()
        )
        parent = f" <- {row['parent_trial']}" if row["parent_trial"] else ""
        flag = "  [needs attention]" if row.get("needs_attention") else ""
        repairs = f"  ({row['repairs']} repaired)" if row.get("repairs") else ""
        print(f"    {row['trial']}{parent}: {row['status']}  {metrics}{repairs}{flag}")

    attention = [r["trial"] for r in index["trials"] if r.get("needs_attention")]
    if attention:
        print(f"\n  {len(attention)} trial(s) need attention: {', '.join(attention)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="autoresearch")
    sub = ap.add_subparsers(dest="command", required=True)

    p_val = sub.add_parser("validate", help="check a campaign config")
    p_val.add_argument("config")
    p_val.set_defaults(func=cmd_validate)

    p_run = sub.add_parser("run-phase", help="run one phase, no campaign")
    p_run.add_argument("config")
    p_run.add_argument("phase")
    p_run.add_argument("--workspace", required=True)
    p_run.add_argument("--out", required=True)
    p_run.add_argument("--tag", default=None)
    p_run.add_argument("--poll-interval", type=float, default=0.5)
    p_run.set_defaults(func=cmd_run_phase)

    p_one = sub.add_parser("run-one", help="run one idea through the workflow")
    p_one.add_argument("config")
    p_one.add_argument("--campaign-dir", required=True)
    p_one.add_argument("--idea", default=None, help="file seeded into the workspace")
    p_one.add_argument("--base-dir", default=None, help="base code state to copy")
    p_one.add_argument("--poll-interval", type=float, default=0.5)
    p_one.set_defaults(func=cmd_run_one)

    p_camp = sub.add_parser("run", help="run a campaign to its budget")
    p_camp.add_argument("config")
    p_camp.add_argument("--campaign-dir", required=True)
    p_camp.add_argument("--idea", default=None, help="file seeded into workspaces")
    p_camp.add_argument(
        "--inject", type=int, default=0, help="inject N ideas into the backlog"
    )
    p_camp.add_argument("--poll-interval", type=float, default=0.5)
    p_camp.add_argument(
        "--harness",
        default=None,
        help="agent harness CLI for agentic phases, e.g. 'claude -p'",
    )
    p_camp.add_argument(
        "--skill-arg", default=None, help="flag the harness takes per skill"
    )
    p_camp.add_argument(
        "--ideate", action="store_true", help="let the harness generate ideas"
    )
    p_camp.add_argument(
        "--vcs", default=None, choices=["hg", "git", "copy"],
        help="version control for trial workspaces",
    )
    p_camp.add_argument("--repo", default=None, help="repository the trials branch from")
    p_camp.add_argument(
        "--mirror", default=None, help="durability tier to mirror the campaign to"
    )
    p_camp.add_argument("--sync-interval", type=float, default=30.0)
    p_camp.add_argument(
        "--no-repair", action="store_true",
        help="don't consult a repair agent when a job hits a no-rule situation",
    )
    p_camp.set_defaults(func=cmd_run)

    p_status = sub.add_parser("status", help="show a campaign's current state")
    p_status.add_argument("campaign_dir")
    p_status.set_defaults(func=cmd_status)

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
