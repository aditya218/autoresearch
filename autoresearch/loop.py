"""The campaign loop: two cooperating loops around `run_trial`.

The ideator keeps a backlog of ideas topped up; the trial runner admits trials
while budgets allow and drives each one to a terminal state. Neither knows how
a trial actually works - that is `run_trial`'s business (§6, §8).

Every admission decision is derived from the replayed ledger, never from a
counter held in memory, so "resume after a crash" and "resume with a raised
budget" are the same code path as ordinary operation.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from autoresearch import events as ev
from autoresearch.campaign import Campaign
from autoresearch.engine import run_trial
from autoresearch.state import IdeaState

BASELINE_TRIAL = "T000"


class Ideator(Protocol):
    """Generates ideas from the research so far. The engine only requires
    that it returns idea payloads; how it thinks is entirely its business."""

    def __call__(self, campaign: Campaign, wanted: int) -> list[dict]:
        ...


@dataclass
class LoopResult:
    status: str  # budget_reached | baseline_failed | stalled_ideation | stopped
    completed: list[str]
    baseline: str | None = None


class CampaignLoop:
    def __init__(
        self,
        campaign: Campaign,
        ideator: Ideator | None = None,
        run_agentic: Callable | None = None,
        poll_interval: float = 5.0,
        max_ideation_failures: int = 3,
        repair_agent=None,
    ):
        self.c = campaign
        self.ideator = ideator
        self.run_agentic = run_agentic
        self.repair_agent = repair_agent
        self.poll_interval = poll_interval
        #: how many times ideation may fail back-to-back with nothing left to
        #: run before the campaign stops rather than spinning
        self.max_ideation_failures = max_ideation_failures
        self._ideation_failures = 0
        self._running: dict[str, asyncio.Task] = {}

    # -- admission (all counts derived from replayed state) ------------------

    @property
    def budget(self):
        return self.c.config.budget

    def budget_exhausted(self) -> bool:
        state = self.c.state
        return (
            state.budget_consumed_trials + state.in_flight_trials
            >= self.budget.max_trials
        )

    def can_admit(self) -> bool:
        state = self.c.state
        if state.status != "running":
            return False
        if state.in_flight_trials >= self.budget.active_trials:
            return False
        if self.budget_exhausted():
            return False
        return bool(state.backlog)

    # -- ideation ------------------------------------------------------------

    def top_up_backlog(self) -> int:
        """Ask the ideator for enough ideas to reach the target. Ideation
        failures are isolated: the backlog simply drains and trials continue."""
        if self.ideator is None:
            return 0
        wanted = self.c.config.ideation.backlog_target - len(self.c.state.backlog)
        if wanted <= 0:
            return 0
        try:
            ideas = self.ideator(self.c, wanted)
        except Exception as exc:  # noqa: BLE001 - a bad ideator must not crash the campaign
            self._ideation_failures += 1
            self.c.recorder.record(
                ev.CampaignPaused,
                reason=f"ideation failed ({self._ideation_failures}): {exc}",
            )
            self.c.recorder.record(ev.CampaignResumed)
            return 0
        self._ideation_failures = 0
        for idea in ideas:
            self.inject_idea(
                idea.get("id") or self._next_idea_id(),
                source="ideator",
                parent_trial=idea.get("parent_trial"),
                payload=idea,
            )
        return len(ideas)

    def _next_idea_id(self) -> str:
        return f"I{len(self.c.state.ideas) + 1:03d}"

    def inject_idea(
        self,
        idea_id: str | None = None,
        source: str = "human",
        parent_trial: str | None = None,
        payload: dict | None = None,
    ) -> str:
        """Add an idea to the backlog. Human injection is the same mechanism
        the ideator uses - which is what makes ideation a dial (§8)."""
        idea_id = idea_id or self._next_idea_id()
        self.c.recorder.record(
            ev.IdeaCreated,
            idea=idea_id,
            source=source,
            parent_trial=parent_trial,
            read_seq=self.c.state.last_seq,
        )
        if payload is not None:
            path = self.c.dir / "ideas" / f"{idea_id}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            import json

            path.write_text(json.dumps(payload, indent=2) + "\n")
        return idea_id

    def idea_payload(self, idea_id: str) -> dict:
        import json

        path = self.c.dir / "ideas" / f"{idea_id}.json"
        return json.loads(path.read_text()) if path.exists() else {}

    # -- running trials ------------------------------------------------------

    async def _drive(self, trial_id: str) -> str:
        ctx = self.c.trial_context(
            trial_id, poll_interval=self.poll_interval, run_agentic=self.run_agentic
        )
        ctx.repair_agent = self.repair_agent
        try:
            return await run_trial(ctx)
        finally:
            self._running.pop(trial_id, None)

    def _base_dir_for(self, parent_trial: str | None) -> Path | None:
        if parent_trial is None:
            return self.c.baseline_dir
        parent_ws = self.c.workspace_dir(parent_trial)
        return parent_ws if parent_ws.exists() else self.c.baseline_dir

    def admit(self, idea: IdeaState) -> str:
        trial_id = self.c.next_trial_id()
        payload = self.idea_payload(idea.idea)
        parent = idea.parent_trial
        self.c.create_trial(trial_id, idea=idea.idea, parent_trial=parent)
        ws = self.c.prepare_workspace(trial_id, base_dir=self._base_dir_for(parent))
        if payload:
            import json

            (ws / "idea.json").write_text(json.dumps(payload, indent=2) + "\n")
        (self.c.dir / "trials" / trial_id).mkdir(parents=True, exist_ok=True)
        return trial_id

    # -- baseline ------------------------------------------------------------

    async def run_baseline(self) -> str:
        """T000: the workflow on unmodified baseline code. Smoke test and
        reference point in one - if it fails, the campaign halts (§8)."""
        if BASELINE_TRIAL in self.c.state.trials:
            trial = self.c.state.trials[BASELINE_TRIAL]
            if not trial.in_flight:
                return trial.status
        else:
            self.c.create_trial(BASELINE_TRIAL)
            self.c.prepare_workspace(BASELINE_TRIAL, base_dir=self.c.baseline_dir)
        return await self._drive(BASELINE_TRIAL)

    # -- the loop ------------------------------------------------------------

    async def run(self, max_ticks: int | None = None) -> LoopResult:
        completed: list[str] = []

        baseline_status = await self.run_baseline()
        if baseline_status != "completed":
            self.c.recorder.record(ev.CampaignFinished, reason="baseline_failed")
            return LoopResult("baseline_failed", completed, baseline=baseline_status)

        # Resume anything the log says was still in flight.
        for trial in list(self.c.state.trials.values()):
            if trial.in_flight and trial.trial != BASELINE_TRIAL:
                self._running[trial.trial] = asyncio.create_task(
                    self._drive(trial.trial)
                )

        ticks = 0
        while True:
            ticks += 1
            if max_ticks is not None and ticks > max_ticks:
                return LoopResult("stopped", completed)

            if self.c.state.status == "running":
                self.top_up_backlog()
                while self.can_admit():
                    idea = self.c.state.backlog[0]
                    trial_id = self.admit(idea)
                    self._running[trial_id] = asyncio.create_task(
                        self._drive(trial_id)
                    )

            if not self._running:
                if self.budget_exhausted() and self.c.state.status == "running":
                    self.c.recorder.record(ev.CampaignFinished, reason="budget_reached")
                    return LoopResult("budget_reached", completed)
                if not self.c.state.backlog and self.ideator is None:
                    self.c.recorder.record(ev.CampaignFinished, reason="stopped_by_user")
                    return LoopResult("stopped", completed)
                if self.c.state.status != "running":
                    return LoopResult("stopped", completed)
                if self._ideation_failures >= self.max_ideation_failures:
                    # Nothing running, nothing to run, and ideation keeps
                    # failing: stop for a human rather than spin.
                    self.c.recorder.record(
                        ev.CampaignFinished, reason="stopped_by_user"
                    )
                    return LoopResult("stalled_ideation", completed)
                await asyncio.sleep(self.poll_interval)
                continue

            # At budget: drain what is running, admit nothing more.
            done, _ = await asyncio.wait(
                list(self._running.values()), return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                completed.append(task.result())

    # -- human controls (each is just an event) ------------------------------

    def pause(self, reason: str = "") -> None:
        self.c.recorder.record(ev.CampaignPaused, reason=reason)

    def resume(self) -> None:
        self.c.recorder.record(ev.CampaignResumed)

    def kill_trial(self, trial_id: str, reason: str = "killed by user") -> None:
        task = self._running.get(trial_id)
        if task is not None:
            task.cancel()
        trial = self.c.state.trials.get(trial_id)
        if trial is not None and trial.in_flight:
            self.c.recorder.record(
                ev.TrialFinished, trial=trial_id, status="killed", reason=reason
            )
