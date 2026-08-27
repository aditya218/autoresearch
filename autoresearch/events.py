"""Event types for the campaign ledger.

Every state change in a campaign is exactly one event: one JSON line in the
ledger. A multi-part state change (e.g. a job launch that also moves its phase
into the remote-running state) is modeled as a single event so there is no
window of partially-recorded state. Corrections are new events, never edits.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

SCHEMA_VERSION = 1

TrialStatus = Literal["completed", "gate_stopped", "failed_idea", "errored", "killed"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class BaseEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=1)
    ts: datetime
    v: int = SCHEMA_VERSION


# --- campaign lifecycle -----------------------------------------------------


class CampaignStarted(BaseEvent):
    type: Literal["campaign_started"] = "campaign_started"
    campaign: str


class CampaignPaused(BaseEvent):
    type: Literal["campaign_paused"] = "campaign_paused"
    reason: str = ""


class CampaignResumed(BaseEvent):
    type: Literal["campaign_resumed"] = "campaign_resumed"


class CampaignFinished(BaseEvent):
    type: Literal["campaign_finished"] = "campaign_finished"
    reason: Literal["budget_reached", "baseline_failed", "stopped_by_user"]


# --- ideas ------------------------------------------------------------------


class IdeaCreated(BaseEvent):
    type: Literal["idea_created"] = "idea_created"
    idea: str
    source: Literal["ideator", "human"]
    parent_trial: str | None = None
    # ledger seq the ideator had read up to when generating this idea:
    # auditable "what did the ideator know?"
    read_seq: int | None = None


# --- trials -----------------------------------------------------------------


class TrialCreated(BaseEvent):
    type: Literal["trial_created"] = "trial_created"
    trial: str
    idea: str | None = None  # None for the T000 baseline
    parent_trial: str | None = None
    base_node: str | None = None


class TrialFinished(BaseEvent):
    type: Literal["trial_finished"] = "trial_finished"
    trial: str
    status: TrialStatus
    reason: str = ""


class TrialReclassified(BaseEvent):
    """Correction of a terminal trial's classification (e.g. the analysis
    phase decides a "failed_idea" was actually an infra flake)."""

    type: Literal["trial_reclassified"] = "trial_reclassified"
    trial: str
    status: TrialStatus
    reason: str = ""


# --- phases -----------------------------------------------------------------


class PhaseStarted(BaseEvent):
    type: Literal["phase_started"] = "phase_started"
    trial: str
    phase: str
    attempt: int = 1
    agentic: bool = False


class PhaseCompleted(BaseEvent):
    """Terminal record of one phase attempt, including the metrics it
    produced - one event, so a crash can never separate a phase's outcome
    from its numbers."""

    type: Literal["phase_completed"] = "phase_completed"
    trial: str
    phase: str
    attempt: int = 1
    status: Literal["passed", "failed"]
    metrics: dict[str, float] = Field(default_factory=dict)
    verified: bool = False  # True when produced by a deterministic phase
    notes: str = ""


class PhaseErrored(BaseEvent):
    """Infra error while running a phase (exception, malformed result.json,
    launch script died without a job_id, ...)."""

    type: Literal["phase_errored"] = "phase_errored"
    trial: str
    phase: str
    attempt: int = 1
    error: str = ""


# --- jobs -------------------------------------------------------------------


class JobLaunched(BaseEvent):
    """Records the job_id AND moves its phase into the remote-running state -
    one event, so a crash can never orphan a launched job."""

    type: Literal["job_launched"] = "job_launched"
    trial: str
    phase: str
    job_id: str
    tag: str = ""


class JobStatusChanged(BaseEvent):
    """A poll observed the job in a new state (transitions only, not every
    poll)."""

    type: Literal["job_status_changed"] = "job_status_changed"
    trial: str
    phase: str
    job_id: str
    status: str


# --- metrics ----------------------------------------------------------------


class JobCancelled(BaseEvent):
    """The engine stopped a job it had launched (killed trial, repair gave
    up, or a relaunch replacing it)."""

    type: Literal["job_cancelled"] = "job_cancelled"
    trial: str
    phase: str
    job_id: str
    reason: str = ""


class RepairStarted(BaseEvent):
    """The engine hit a situation it has no rule for and asked repair."""

    type: Literal["repair_started"] = "repair_started"
    trial: str
    phase: str
    trigger: str
    detail: str = ""
    job_id: str | None = None


class RepairVerdict(BaseEvent):
    """What repair recommended. `diagnosis` is prose and inert; `action` is
    what the engine acted on."""

    type: Literal["repair_verdict"] = "repair_verdict"
    trial: str
    phase: str
    action: str
    diagnosis: str = ""
    job_id: str | None = None


class MetricRecorded(BaseEvent):
    """Standalone metric, or a correction overriding an earlier value."""

    type: Literal["metric_recorded"] = "metric_recorded"
    trial: str
    metric: str
    value: float
    phase: str | None = None
    verified: bool = False


Event = Annotated[
    Union[
        CampaignStarted,
        CampaignPaused,
        CampaignResumed,
        CampaignFinished,
        IdeaCreated,
        TrialCreated,
        TrialFinished,
        TrialReclassified,
        PhaseStarted,
        PhaseCompleted,
        PhaseErrored,
        JobLaunched,
        JobStatusChanged,
        JobCancelled,
        RepairStarted,
        RepairVerdict,
        MetricRecorded,
    ],
    Field(discriminator="type"),
]

_event_adapter: TypeAdapter[Event] = TypeAdapter(Event)


def parse_event(line: str | bytes) -> Event:
    return _event_adapter.validate_json(line)


def dump_event(event: BaseEvent) -> str:
    return event.model_dump_json()
