"""Derived in-memory campaign state - a pure fold over the event log.

Disposable by design: replaying the ledger rebuilds it exactly. Budgets and
counters are computed from this state, never stored anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from autoresearch import events as ev


class StateError(Exception):
    """An event arrived that is inconsistent with current state (engine bug
    or corrupt ledger - either way, stop rather than guess)."""


@dataclass
class Metric:
    value: float
    verified: bool = False
    phase: str | None = None


@dataclass
class PhaseState:
    name: str
    status: str = "running"  # running | passed | failed | errored
    attempt: int = 1
    agentic: bool = False
    job_id: str | None = None
    job_status: str | None = None
    notes: str = ""
    #: how many times repair has been asked about this phase attempt
    repair_attempts: int = 0
    last_repair_action: str | None = None


@dataclass
class TrialState:
    trial: str
    idea: str | None = None
    parent_trial: str | None = None
    base_node: str | None = None
    status: str = "running"  # running | <TrialStatus>
    reason: str = ""
    phases: dict[str, PhaseState] = field(default_factory=dict)
    metrics: dict[str, Metric] = field(default_factory=dict)
    created_seq: int = 0
    updated_seq: int = 0  # seq of the last event touching this trial

    @property
    def in_flight(self) -> bool:
        return self.status == "running"


@dataclass
class IdeaState:
    idea: str
    source: str
    parent_trial: str | None = None
    consumed_by: str | None = None  # trial id, once admitted


# Terminal statuses that consumed an evaluation and count against max_trials.
# Infra-errored and human-killed trials told us nothing about the research.
BUDGET_STATUSES = frozenset({"completed", "gate_stopped", "failed_idea"})


@dataclass
class CampaignState:
    campaign: str | None = None
    status: str = "running"  # running | paused | finished
    finish_reason: str | None = None
    last_seq: int = 0
    ideas: dict[str, IdeaState] = field(default_factory=dict)
    trials: dict[str, TrialState] = field(default_factory=dict)

    # -- derived counters (admission logic reads these) ----------------------

    @property
    def backlog(self) -> list[IdeaState]:
        return [i for i in self.ideas.values() if i.consumed_by is None]

    @property
    def in_flight_trials(self) -> int:
        return sum(1 for t in self.trials.values() if t.in_flight)

    @property
    def budget_consumed_trials(self) -> int:
        return sum(1 for t in self.trials.values() if t.status in BUDGET_STATUSES)

    @property
    def launched_job_ids(self) -> list[str]:
        """Job ids of phases currently running remotely - what a restart must
        reattach to."""
        return [
            p.job_id
            for t in self.trials.values()
            if t.in_flight
            for p in t.phases.values()
            if p.status == "running" and p.job_id is not None
        ]

    # -- the fold ------------------------------------------------------------

    @classmethod
    def replay(cls, event_stream: Iterable[ev.Event]) -> "CampaignState":
        state = cls()
        for event in event_stream:
            state.apply(event)
        return state

    def apply(self, event: ev.Event) -> None:
        if event.seq != self.last_seq + 1:
            raise StateError(
                f"sequence gap: expected {self.last_seq + 1}, got {event.seq}"
            )
        handler = getattr(self, f"_on_{event.type}", None)
        if handler is None:
            raise StateError(f"no handler for event type {event.type!r}")
        handler(event)
        self.last_seq = event.seq

    # -- helpers -------------------------------------------------------------

    def _trial(self, trial_id: str) -> TrialState:
        try:
            return self.trials[trial_id]
        except KeyError:
            raise StateError(f"event references unknown trial {trial_id!r}") from None

    def _phase(self, trial_id: str, phase: str) -> PhaseState:
        trial = self._trial(trial_id)
        try:
            return trial.phases[phase]
        except KeyError:
            raise StateError(
                f"event references unknown phase {phase!r} of trial {trial_id!r}"
            ) from None

    def _touch(self, trial_id: str, seq: int) -> None:
        self.trials[trial_id].updated_seq = seq

    # -- campaign ------------------------------------------------------------

    def _on_campaign_started(self, e: ev.CampaignStarted) -> None:
        self.campaign = e.campaign
        self.status = "running"

    def _on_campaign_paused(self, e: ev.CampaignPaused) -> None:
        self.status = "paused"

    def _on_campaign_resumed(self, e: ev.CampaignResumed) -> None:
        self.status = "running"

    def _on_campaign_finished(self, e: ev.CampaignFinished) -> None:
        self.status = "finished"
        self.finish_reason = e.reason

    # -- ideas ---------------------------------------------------------------

    def _on_idea_created(self, e: ev.IdeaCreated) -> None:
        if e.idea in self.ideas:
            raise StateError(f"duplicate idea id {e.idea!r}")
        self.ideas[e.idea] = IdeaState(
            idea=e.idea, source=e.source, parent_trial=e.parent_trial
        )

    # -- trials --------------------------------------------------------------

    def _on_trial_created(self, e: ev.TrialCreated) -> None:
        if e.trial in self.trials:
            raise StateError(f"duplicate trial id {e.trial!r}")
        if e.idea is not None:
            idea = self.ideas.get(e.idea)
            if idea is None:
                raise StateError(f"trial {e.trial!r} references unknown idea {e.idea!r}")
            if idea.consumed_by is not None:
                raise StateError(
                    f"idea {e.idea!r} already consumed by {idea.consumed_by!r}"
                )
            idea.consumed_by = e.trial
        self.trials[e.trial] = TrialState(
            trial=e.trial,
            idea=e.idea,
            parent_trial=e.parent_trial,
            base_node=e.base_node,
            created_seq=e.seq,
            updated_seq=e.seq,
        )

    def _on_trial_finished(self, e: ev.TrialFinished) -> None:
        trial = self._trial(e.trial)
        if not trial.in_flight:
            raise StateError(f"trial {e.trial!r} finished twice")
        trial.status = e.status
        trial.reason = e.reason
        self._touch(e.trial, e.seq)

    def _on_trial_reclassified(self, e: ev.TrialReclassified) -> None:
        trial = self._trial(e.trial)
        if trial.in_flight:
            raise StateError(f"cannot reclassify in-flight trial {e.trial!r}")
        trial.status = e.status
        trial.reason = e.reason
        self._touch(e.trial, e.seq)

    # -- phases --------------------------------------------------------------

    def _on_phase_started(self, e: ev.PhaseStarted) -> None:
        trial = self._trial(e.trial)
        # A new attempt replaces the previous attempt's phase state.
        trial.phases[e.phase] = PhaseState(
            name=e.phase, attempt=e.attempt, agentic=e.agentic
        )
        self._touch(e.trial, e.seq)

    def _on_phase_completed(self, e: ev.PhaseCompleted) -> None:
        phase = self._phase(e.trial, e.phase)
        phase.status = e.status
        phase.notes = e.notes
        trial = self.trials[e.trial]
        for name, value in e.metrics.items():
            trial.metrics[name] = Metric(value=value, verified=e.verified, phase=e.phase)
        self._touch(e.trial, e.seq)

    def _on_phase_errored(self, e: ev.PhaseErrored) -> None:
        phase = self._phase(e.trial, e.phase)
        phase.status = "errored"
        self._touch(e.trial, e.seq)

    # -- jobs ----------------------------------------------------------------

    def _on_job_launched(self, e: ev.JobLaunched) -> None:
        phase = self._phase(e.trial, e.phase)
        phase.job_id = e.job_id
        phase.job_status = "launched"
        self._touch(e.trial, e.seq)

    def _on_job_status_changed(self, e: ev.JobStatusChanged) -> None:
        phase = self._phase(e.trial, e.phase)
        phase.job_status = e.status
        self._touch(e.trial, e.seq)

    # -- repair --------------------------------------------------------------

    def _on_repair_started(self, e: ev.RepairStarted) -> None:
        phase = self._phase(e.trial, e.phase)
        phase.repair_attempts += 1
        self._touch(e.trial, e.seq)

    def _on_repair_verdict(self, e: ev.RepairVerdict) -> None:
        phase = self._phase(e.trial, e.phase)
        phase.last_repair_action = e.action
        self._touch(e.trial, e.seq)

    # -- metrics -------------------------------------------------------------

    def _on_metric_recorded(self, e: ev.MetricRecorded) -> None:
        trial = self._trial(e.trial)
        trial.metrics[e.metric] = Metric(
            value=e.value, verified=e.verified, phase=e.phase
        )
        self._touch(e.trial, e.seq)
