"""State machines. Doc 03.

Legal transitions are data, not scattered conditionals, so an illegal move is
rejected in one place rather than producing an impossible state at 3am.
"""
from __future__ import annotations

CAMPAIGN = {
    "DRAFT": {"ACTIVE"},
    "ACTIVE": {"PAUSED", "STOPPING"},
    "PAUSED": {"ACTIVE", "STOPPING"},
    "STOPPING": {"COMPLETED"},
    "COMPLETED": {"ARCHIVED"},
    "ARCHIVED": set(),
}

RUN = {
    "ACTIVE": {"DRAINING", "ENDED"},
    "DRAINING": {"ENDED"},
    "ENDED": set(),
}

HYPOTHESIS = {
    "PROPOSED": {"QUEUED", "REJECTED"},
    "QUEUED": {"CLAIMED", "EXPIRED", "SUPERSEDED"},
    "CLAIMED": {"MATERIALIZED", "QUEUED"},   # back to QUEUED when a claim lapses
    "MATERIALIZED": set(),
    "REJECTED": set(),
    "SUPERSEDED": set(),
    "EXPIRED": set(),
}

EXPERIMENT = {
    "CREATED": {"ADMITTED", "ABORTED"},
    "ADMITTED": {"RUNNING", "ABORTED"},
    "RUNNING": {"AGGREGATING", "FAILED", "ABORTED"},
    "AGGREGATING": {"SUCCEEDED", "FAILED"},
    "SUCCEEDED": set(),
    "FAILED": set(),
    "ABORTED": set(),
}

REPLICATE = {
    "PENDING": {"RUNNING", "CANCELLED"},
    "RUNNING": {"COMPLETED", "FAILED", "CANCELLED"},
    "COMPLETED": set(),
    "FAILED": set(),
    "CANCELLED": set(),
}

# LAUNCH_INTENT is the one ambiguous state: the side effect may or may not have
# happened. Recovery resolves it (doc 04 §2).
STAGE = {
    "PENDING": {"LAUNCH_INTENT", "CANCELLED"},
    # LAUNCHED for external jobs (a handle to record); RUNNING directly for
    # local stages, which have no handle.
    "LAUNCH_INTENT": {"LAUNCHED", "RUNNING", "FAILED", "CANCELLED"},
    "LAUNCHED": {"RUNNING", "COMPLETED", "FAILED", "CANCELLED"},
    "RUNNING": {"COMPLETED", "FAILED", "CANCELLED"},
    "COMPLETED": set(),
    "FAILED": set(),
    "CANCELLED": set(),
}

MACHINES = {
    "campaign": CAMPAIGN,
    "run": RUN,
    "hypothesis": HYPOTHESIS,
    "experiment": EXPERIMENT,
    "replicate": REPLICATE,
    "stage_execution": STAGE,
}

TABLES = {
    "campaign": ("campaign", "campaign_id", "status"),
    "run": ("run", "run_id", "status"),
    "hypothesis": ("hypothesis", "hypothesis_id", "state"),
    "experiment": ("experiment", "experiment_id", "state"),
    "replicate": ("replicate", "replicate_id", "state"),
    "stage_execution": ("stage_execution", "stage_execution_id", "state"),
}

TERMINAL = {
    "experiment": {"SUCCEEDED", "FAILED", "ABORTED"},
    "replicate": {"COMPLETED", "FAILED", "CANCELLED"},
    "stage_execution": {"COMPLETED", "FAILED", "CANCELLED"},
}

# Stages here may or may not correspond to a live external effect.
STAGE_NON_TERMINAL = {"PENDING", "LAUNCH_INTENT", "LAUNCHED", "RUNNING"}


class IllegalTransition(Exception):
    pass


class StaleFence(Exception):
    """This run's lease was seized. It has no authority to write."""


def check(entity_type: str, frm: str, to: str) -> None:
    machine = MACHINES[entity_type]
    if frm not in machine:
        raise IllegalTransition(f"{entity_type}: unknown state {frm!r}")
    if to not in machine[frm]:
        raise IllegalTransition(f"{entity_type}: {frm} -> {to} is not legal")
