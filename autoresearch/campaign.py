"""Opening a campaign directory: ledger, replayed state, views, workspaces.

Opening is the same operation whether a campaign is new, resuming after a
clean exit, or recovering from a crash: replay the log, regenerate stale
views, and carry on. There is no separate recovery path to get wrong.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from autoresearch import events as ev
from autoresearch.config import CampaignConfig, load_config
from autoresearch.engine import Recorder, TrialContext
from autoresearch.ledger import Ledger
from autoresearch.project import Project
from autoresearch.state import CampaignState
from autoresearch.views import ViewWriter


@dataclass
class OpenReport:
    """What opening the campaign found - surfaced so operators can see that a
    crash was recovered from rather than silently papered over."""

    recovered_bytes: int
    regenerated_views: list[str]
    resumed_trials: list[str]
    reattached_jobs: list[str]


class Campaign:
    def __init__(
        self,
        campaign_dir: str | Path,
        config_path: str | Path | None = None,
        vcs=None,
        sync=None,
    ):
        self.dir = Path(campaign_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.vcs = vcs
        self.sync = sync
        self._workspaces: dict[str, object] = {}
        self.config_path = Path(config_path) if config_path else self.dir / "campaign.yaml"
        self.config: CampaignConfig = load_config(self.config_path)

        project_dir = Path(self.config.project_dir)
        if not project_dir.is_absolute():
            project_dir = (self.config_path.parent / project_dir).resolve()
        self.project = Project(project_dir)

        self.ledger = Ledger(self.dir / "ledger" / "events.jsonl")
        self.state = CampaignState.replay(self.ledger.events())
        self.views = ViewWriter(self.dir)
        self.recorder = Recorder(self.ledger, self.state, self.views, sync=sync)

        regenerated = self.views.regenerate_stale(self.state)
        self.report = OpenReport(
            recovered_bytes=self.ledger.recovered_bytes,
            regenerated_views=regenerated,
            resumed_trials=[
                t.trial for t in self.state.trials.values() if t.in_flight
            ],
            reattached_jobs=list(self.state.launched_job_ids),
        )

        if self.state.campaign is None:
            self.recorder.record(ev.CampaignStarted, campaign=self.config.name)

    # -- workspaces ----------------------------------------------------------

    @property
    def baseline_dir(self) -> Path | None:
        """The unmodified code state trials branch from. Stand-in for a VCS
        node (§6.4): `base_code` beside the config, if the project has one."""
        candidate = self.config_path.parent / "base_code"
        return candidate if candidate.exists() else None

    def workspace_dir(self, trial_id: str) -> Path:
        return self.dir / "workspaces" / trial_id

    def prepare_workspace(
        self,
        trial_id: str,
        base_dir: Path | None = None,
        base_node: str | None = None,
    ) -> Path:
        """Materialize a trial's workspace through the VCS adapter."""
        ws = self.workspace_dir(trial_id)
        if ws.exists():
            return ws
        ws.parent.mkdir(parents=True, exist_ok=True)
        if self.vcs is not None:
            workspace = self.vcs.workspace_acquire(base_node, trial_id)
            self._workspaces[trial_id] = workspace
            return workspace.path
        # No VCS configured: copy the base state directly.
        if base_dir is not None and Path(base_dir).exists():
            shutil.copytree(base_dir, ws)
        else:
            ws.mkdir(parents=True)
        return ws

    def commit_workspace(self, trial_id: str, message: str) -> str | None:
        """Record a trial's code state as a node, so later trials can branch
        from it. Returns the node, or None without a VCS."""
        workspace = self._workspaces.get(trial_id)
        if self.vcs is None or workspace is None:
            return None
        return self.vcs.commit_all(workspace, message)

    def release_workspace(self, trial_id: str) -> None:
        workspace = self._workspaces.pop(trial_id, None)
        if self.vcs is not None and workspace is not None:
            self.vcs.workspace_release(workspace)

    # -- trials --------------------------------------------------------------

    def create_trial(
        self,
        trial_id: str,
        idea: str | None = None,
        parent_trial: str | None = None,
        base_node: str | None = None,
    ) -> None:
        self.recorder.record(
            ev.TrialCreated, trial=trial_id, idea=idea,
            parent_trial=parent_trial, base_node=base_node,
        )

    def next_trial_id(self) -> str:
        """Lowest unused id. T000 is reserved for the baseline, so ordinary
        trials start at T001 whether or not a baseline has run."""
        used = set(self.state.trials)
        n = 0 if "T000" not in used else 1
        while f"T{n:03d}" in used:
            n += 1
        return f"T{max(n, 0):03d}"

    def trial_context(
        self,
        trial_id: str,
        workspace: Path | None = None,
        poll_interval: float = 5.0,
        run_agentic=None,
    ) -> TrialContext:
        return TrialContext(
            trial_id=trial_id,
            campaign_dir=self.dir,
            workspace=workspace or self.workspace_dir(trial_id),
            config=self.config,
            project=self.project,
            recorder=self.recorder,
            poll_interval=poll_interval,
            run_agentic=run_agentic,
        )

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        self.ledger.close()

    def __enter__(self) -> "Campaign":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
