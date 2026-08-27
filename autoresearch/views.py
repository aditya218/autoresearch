"""Materialized views - the readable, disposable projection of the ledger.

The engine rewrites the affected view files on every state transition (atomic
temp-file + rename), so agents and humans always read plain, current JSON.
Each view records the seq it was materialized at; on startup any view older
than its trial's last change is regenerated, which also heals a crash between
event append and view write. Views are never read back by the engine as state.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from autoresearch.state import CampaignState, TrialState


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n")
    os.replace(tmp, path)


def trial_view(trial: TrialState) -> dict:
    return {
        "trial": trial.trial,
        "status": trial.status,
        "reason": trial.reason,
        "idea": trial.idea,
        "parent_trial": trial.parent_trial,
        "base_node": trial.base_node,
        "phases": {
            name: {
                "status": p.status,
                "attempt": p.attempt,
                "agentic": p.agentic,
                "job_id": p.job_id,
                "job_status": p.job_status,
                "notes": p.notes,
                **(
                    {
                        "repair_attempts": p.repair_attempts,
                        "last_repair_action": p.last_repair_action,
                    }
                    if p.repair_attempts
                    else {}
                ),
            }
            for name, p in trial.phases.items()
        },
        "metrics": {
            name: {"value": m.value, "verified": m.verified, "phase": m.phase}
            for name, m in trial.metrics.items()
        },
        "materialized_seq": trial.updated_seq,
    }


def index_view(state: CampaignState) -> dict:
    return {
        "campaign": state.campaign,
        "status": state.status,
        "finish_reason": state.finish_reason,
        "backlog": [i.idea for i in state.backlog],
        "trials": [
            {
                "trial": t.trial,
                "parent_trial": t.parent_trial,
                "status": t.status,
                "idea": t.idea,
                "metrics": {
                    name: {"value": m.value, "verified": m.verified}
                    for name, m in t.metrics.items()
                },
                # "needs attention" means a human must look - a trial that
                # repair rescued does not qualify, or every rescue would
                # raise a false alarm. Repairs stay visible per phase.
                **({"needs_attention": True} if t.status == "errored" else {}),
                **(
                    {"repairs": sum(p.repair_attempts for p in t.phases.values())}
                    if any(p.repair_attempts for p in t.phases.values())
                    else {}
                ),
            }
            for t in sorted(state.trials.values(), key=lambda t: t.created_seq)
        ],
        "materialized_seq": state.last_seq,
    }


class ViewWriter:
    def __init__(self, campaign_dir: str | Path):
        self.campaign_dir = Path(campaign_dir)

    def trial_path(self, trial_id: str) -> Path:
        return self.campaign_dir / "trials" / trial_id / "trial.json"

    @property
    def index_path(self) -> Path:
        return self.campaign_dir / "index" / "trials.json"

    # -- on-write updates (called after each applied event) ------------------

    def write_trial(self, trial: TrialState) -> None:
        _write_atomic(self.trial_path(trial.trial), trial_view(trial))

    def write_index(self, state: CampaignState) -> None:
        _write_atomic(self.index_path, index_view(state))

    # -- startup regeneration ------------------------------------------------

    def _materialized_seq(self, path: Path) -> int:
        """Seq a view file was written at; 0 if missing or unreadable (a
        corrupt or hand-mangled view is simply stale - regenerate it)."""
        try:
            return int(json.loads(path.read_text())["materialized_seq"])
        except Exception:
            return 0

    def regenerate_stale(self, state: CampaignState) -> list[str]:
        """Bring every view up to date with the replayed state; returns what
        was rewritten (trial ids, plus "index")."""
        rewritten = []
        for trial in state.trials.values():
            if self._materialized_seq(self.trial_path(trial.trial)) < trial.updated_seq:
                self.write_trial(trial)
                rewritten.append(trial.trial)
        if self._materialized_seq(self.index_path) < state.last_seq:
            self.write_index(state)
            rewritten.append("index")
        return rewritten
