"""Running one trial: the workflow DAG walk.

`run_trial` knows nothing about campaigns, ideators, budgets, or admission -
that is the whole point of the boundary (§6). It walks the phase DAG, writes
every transition to the ledger, keeps the views current, and returns how the
trial ended.

Resume falls out of the same code: the walk asks derived state "is this phase
already done?" rather than tracking progress itself, so a restart after replay
re-enters the walk and simply skips what the log says finished.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from autoresearch import events as ev
from autoresearch.config import CampaignConfig, PhaseConfig
from autoresearch.ledger import Ledger
from autoresearch.phases import JobPhase, PhaseFailure, PhaseOutcome, run_local_phase
from autoresearch.project import DONE, FAILED, KNOWN_STATUSES, PENDING, Project
from autoresearch.state import CampaignState
from autoresearch.views import ViewWriter


@dataclass
class Recorder:
    """The engine's single writing path: append the event, update the derived
    state, refresh the affected views. Nothing else writes the ledger."""

    ledger: Ledger
    state: CampaignState
    views: ViewWriter
    #: optional CampaignSync - a job launch is mirrored immediately rather
    #: than waiting for the next pass, so a lost disk cannot orphan a job
    sync: object | None = None

    def record(self, event_cls, **fields):
        event = self.ledger.append(event_cls, **fields)
        self.state.apply(event)
        trial_id = getattr(event, "trial", None)
        if trial_id is not None and trial_id in self.state.trials:
            self.views.write_trial(self.state.trials[trial_id])
        self.views.write_index(self.state)
        if self.sync is not None and event.type == "job_launched":
            self.sync.push_now()
        return event


@dataclass
class TrialContext:
    """Everything one trial needs, prepared by the caller."""

    trial_id: str
    campaign_dir: Path
    workspace: Path
    config: CampaignConfig
    project: Project
    recorder: Recorder
    poll_interval: float = 5.0
    #: agentic phases are executed by an injected callable:
    #: (phase, PhaseConfig, workspace, phase_dir) -> PhaseOutcome
    run_agentic: object | None = None
    #: repair.RepairAgent, consulted when a job phase hits a situation the
    #: engine has no rule for (§9.1). Without one, such a phase simply fails.
    repair_agent: object | None = None
    #: how long a job may sit in one status before repair is asked to look
    stuck_after_polls: int = 0

    @property
    def state(self) -> CampaignState:
        return self.recorder.state

    def phase_dir(self, phase: str) -> Path:
        return self.campaign_dir / "trials" / self.trial_id / "phases" / phase

    def tag(self, phase: str) -> str:
        return f"{self.config.name}/{self.trial_id}/{phase}"


class AgenticPhaseUnsupported(PhaseFailure):
    """No agent harness was supplied for an agentic phase."""


async def _run_one_phase(
    ctx: TrialContext,
    phase: str,
    cfg: PhaseConfig,
    attempt: int,
    resume_job: tuple[str, str | None] | None = None,
) -> PhaseOutcome:
    """Execute a single attempt of one phase. Raises PhaseFailure on infra
    errors; a phase that runs and reports `failed` returns normally."""
    phase_dir = ctx.phase_dir(phase)
    phase_dir.mkdir(parents=True, exist_ok=True)

    if cfg.agentic:
        if ctx.run_agentic is None:
            raise AgenticPhaseUnsupported(
                f"{phase}: agentic phase needs an agent harness"
            )
        return await asyncio.to_thread(
            ctx.run_agentic, phase, cfg, ctx.workspace, phase_dir
        )

    if cfg.uses != "job":
        return await asyncio.to_thread(
            run_local_phase, ctx.project, phase, cfg, ctx.workspace, phase_dir
        )

    # Remote job: record the job_id the instant launch returns, before any
    # further work - that single event is what makes a job un-loseable.
    job = JobPhase(
        project=ctx.project, phase=phase, cfg=cfg, workspace=ctx.workspace,
        phase_dir=phase_dir, tag=ctx.tag(phase),
    )
    if resume_job is not None:
        # Resuming after a restart: reattach instead of launching again.
        job.job_id, job.status = resume_job
    else:
        job_id = await asyncio.to_thread(job.launch)
        ctx.recorder.record(
            ev.JobLaunched, trial=ctx.trial_id, phase=phase,
            job_id=job_id, tag=job.tag,
        )

    last_status = job.status
    same_status_polls = 0
    while True:
        status = await asyncio.to_thread(job.poll)
        if status != last_status:
            ctx.recorder.record(
                ev.JobStatusChanged, trial=ctx.trial_id, phase=phase,
                job_id=job.job_id, status=status,
            )
            last_status = status
            same_status_polls = 0
        else:
            same_status_polls += 1

        if status in (DONE, FAILED):
            break

        # Three situations the engine has no rule for: a status it doesn't
        # understand, a job that never starts, and one that runs without its
        # status ever moving (§9.1).
        if status == PENDING:
            # A queued job has consumed nothing yet, so it gets its own
            # patience: never scheduling is a different problem from running
            # a long time, and usually wants a different answer.
            limit = (
                cfg.pending_after_polls
                or cfg.stuck_after_polls
                or ctx.stuck_after_polls
            )
            trigger = "never_scheduled"
        else:
            limit = cfg.stuck_after_polls or ctx.stuck_after_polls
            trigger = "stuck"
        stuck = limit > 0 and same_status_polls >= limit

        if status not in KNOWN_STATUSES or stuck:
            if status not in KNOWN_STATUSES:
                trigger = "unknown_status"
            action = await _ask_repair(
                ctx, phase, cfg, job, trigger,
                detail=f"status {status!r} after {same_status_polls} identical polls",
            )
            if action == "wait":
                same_status_polls = 0
            elif action == "collect":
                break
            elif action == "relaunch":
                return await _run_one_phase(ctx, phase, cfg, attempt)
            elif action == "fail_idea":
                return PhaseOutcome(status="failed", notes="repair: idea failed")
            else:  # fail_infra or escalate
                raise PhaseFailure(f"{phase}: repair gave up ({action})")

        await asyncio.sleep(ctx.poll_interval)

    try:
        outcome = await asyncio.to_thread(job.collect)
    except PhaseFailure:
        action = await _ask_repair(
            ctx, phase, cfg, job, "collect_failed",
            detail="collect produced no usable result",
        )
        if action != "relaunch":
            raise
        return await _run_one_phase(ctx, phase, cfg, attempt)
    return outcome


async def _ask_repair(ctx: TrialContext, phase: str, cfg, job, trigger: str, detail: str) -> str:
    """Consult the repair agent about a no-rule situation and record what it
    said. The engine, not the agent, performs whatever comes next."""
    from autoresearch.repair import Situation, max_attempts_for

    if ctx.repair_agent is None:
        raise PhaseFailure(f"{phase}: {trigger} ({detail})")

    attempts = ctx.state.trials[ctx.trial_id].phases[phase].repair_attempts
    if attempts >= max(max_attempts_for(cfg), 1):
        raise PhaseFailure(f"{phase}: repair exhausted after {attempts} attempts")

    situation = Situation(
        trial_id=ctx.trial_id, phase=phase, trigger=trigger, detail=detail,
        job_id=job.job_id, status=job.status,
        phase_dir=ctx.phase_dir(phase), workspace=ctx.workspace,
        history=_job_history(ctx, phase),
    )
    ctx.recorder.record(
        ev.RepairStarted, trial=ctx.trial_id, phase=phase,
        trigger=trigger, detail=detail, job_id=job.job_id,
    )
    try:
        verdict = await asyncio.to_thread(ctx.repair_agent.diagnose, situation)
    except Exception as exc:  # noqa: BLE001 - a failed repair is not a fix
        ctx.recorder.record(
            ev.RepairVerdict, trial=ctx.trial_id, phase=phase,
            action="escalate", diagnosis=f"repair failed: {exc}",
        )
        raise PhaseFailure(f"{phase}: repair failed: {exc}") from exc

    ctx.recorder.record(
        ev.RepairVerdict, trial=ctx.trial_id, phase=phase,
        action=str(verdict.action), diagnosis=verdict.diagnosis,
        job_id=job.job_id,
    )
    return str(verdict.action)


def _job_history(ctx: TrialContext, phase: str) -> list[dict]:
    from autoresearch.repair import job_history

    return job_history(ctx.state, ctx.trial_id, phase)


async def _attempt_phase(ctx: TrialContext, phase: str, cfg: PhaseConfig) -> PhaseOutcome | None:
    """Run a phase with retries. Returns the outcome, or None when every
    attempt hit an infra error."""
    # Read any in-flight job *before* recording phase_started, which starts a
    # fresh attempt and clears the phase's job state.
    recorded = ctx.state.trials[ctx.trial_id].phases.get(phase)
    resume_job = (
        (recorded.job_id, recorded.job_status)
        if recorded is not None and recorded.job_id and recorded.status == "running"
        else None
    )

    for attempt in range(1, cfg.max_retries + 2):
        ctx.recorder.record(
            ev.PhaseStarted, trial=ctx.trial_id, phase=phase,
            attempt=attempt, agentic=cfg.agentic,
        )
        try:
            outcome = await _run_one_phase(ctx, phase, cfg, attempt, resume_job)
            resume_job = None  # a retry is a fresh launch, never a reattach
        except PhaseFailure as exc:
            ctx.recorder.record(
                ev.PhaseErrored, trial=ctx.trial_id, phase=phase,
                attempt=attempt, error=str(exc),
            )
            continue
        ctx.recorder.record(
            ev.PhaseCompleted, trial=ctx.trial_id, phase=phase, attempt=attempt,
            status=outcome.status, metrics=_allowed_metrics(ctx, phase, outcome),
            verified=outcome.verified, notes=outcome.notes,
        )
        return outcome
    return None


def _allowed_metrics(ctx: TrialContext, phase: str, outcome: PhaseOutcome) -> dict:
    """Provenance enforcement (§7.3): a key metric may only be recorded by the
    deterministic phase it is bound to. Other numbers still land, unverified."""
    allowed = {}
    for name, value in outcome.metrics.items():
        binding = ctx.config.key_metrics.get(name)
        if binding is not None and binding.from_phase != phase:
            continue  # not this phase's metric to report
        allowed[name] = value
    return allowed


async def run_trial(ctx: TrialContext) -> str:
    """Walk the workflow to a terminal state; returns the trial status.

    Assumes `trial_created` is already recorded (the caller decides whether a
    trial should exist - admission is not this function's business).
    """
    trial = ctx.state.trials[ctx.trial_id]
    if not trial.in_flight:
        return trial.status  # already terminal: resuming it is a no-op

    phase = ctx.config.root_phase

    while phase is not None:
        cfg = ctx.config.workflow[phase]
        recorded = trial.phases.get(phase)

        # Resume: a phase the log says passed is never re-run.
        if recorded is not None and recorded.status == "passed":
            outcome_status = "passed"
        else:
            outcome = await _attempt_phase(ctx, phase, cfg)
            if outcome is None:
                ctx.recorder.record(
                    ev.TrialFinished, trial=ctx.trial_id, status="errored",
                    reason=f"phase {phase} exhausted retries",
                )
                return "errored"
            outcome_status = outcome.status

        if outcome_status == "failed":
            if cfg.gate:
                ctx.recorder.record(
                    ev.TrialFinished, trial=ctx.trial_id, status="gate_stopped",
                    reason=f"gate {phase} failed",
                )
                return "gate_stopped"
            ctx.recorder.record(
                ev.TrialFinished, trial=ctx.trial_id, status="failed_idea",
                reason=f"phase {phase} failed",
            )
            return "failed_idea"

        # TODO(dag): one successor, because a workflow is a chain today.
        # Config validation rejects fan-out, so this never silently drops
        # a branch; supporting real parallel phases means replacing this
        # cursor with a ready-set loop over phases whose predecessors have
        # all passed.
        nexts = ctx.config.next_phases(phase)
        phase = nexts[0] if nexts else None

    ctx.recorder.record(ev.TrialFinished, trial=ctx.trial_id, status="completed")
    return "completed"
