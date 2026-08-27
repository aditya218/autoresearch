"""Campaign configuration schema.

The config is the project's whole interface to the engine: what the campaign
is for, which metrics matter, how ideas are generated, and the workflow DAG.
Validation is strict and eager - a bad config should fail at `validate` time,
long before a campaign burns compute on it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ConfigError(Exception):
    """The campaign config is invalid."""


class RepairConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill: str
    max_attempts: int = Field(default=2, ge=0)


class PhaseConfig(BaseModel):
    """One phase of the workflow.

    A phase is either agentic (an agent works in the trial workspace, guided
    by skills) or deterministic (`uses` a shared or custom phase
    implementation). Exactly one of the two.
    """

    model_config = ConfigDict(extra="forbid")

    after: str | None = None
    gate: bool = False

    # agentic
    agentic: bool = False
    skills: list[str] = Field(default_factory=list)
    prompt: str | None = None

    # deterministic
    uses: str | None = None
    params: dict = Field(default_factory=dict)

    # contracts and policy
    produces: list[str] = Field(default_factory=list)
    max_retries: int = Field(default=2, ge=0)
    timeout_s: float | None = Field(default=None, gt=0)
    repair: RepairConfig | None = None
    #: polls a job may sit in one status before repair is asked to look;
    #: 0 disables the stuck check
    stuck_after_polls: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _check_kind(self) -> "PhaseConfig":
        if self.agentic == (self.uses is not None):
            raise ValueError(
                "a phase must be either agentic (agentic: true) or "
                "deterministic (uses: ...), not both or neither"
            )
        if not self.agentic and self.skills:
            raise ValueError("skills are only meaningful on an agentic phase")
        return self

    @property
    def kind(self) -> Literal["agentic", "deterministic"]:
        return "agentic" if self.agentic else "deterministic"


class MetricConfig(BaseModel):
    """A key metric: the numbers gates and ideation are allowed to trust."""

    model_config = ConfigDict(extra="forbid")

    # Phase that is allowed to produce it. Must be deterministic (§7.3).
    from_phase: str = Field(alias="from")
    goal: Literal["maximize", "minimize"] = "maximize"


class IdeationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backlog_target: int = Field(default=3, ge=0)
    skills: list[str] = Field(default_factory=list)
    prompt: str | None = None


class BudgetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_trials: int = Field(default=20, ge=1)
    active_trials: int = Field(default=4, ge=1)


class CampaignConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    goal: str
    #: directory holding the project's scripts and skills - deliberately
    #: outside the trial workspace, so agents cannot rewrite the eval
    #: harness or launchers (§7.3).
    project_dir: str = "project"
    key_metrics: dict[str, MetricConfig] = Field(default_factory=dict)
    ideation: IdeationConfig = Field(default_factory=IdeationConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    workflow: dict[str, PhaseConfig]

    # -- structural validation ----------------------------------------------

    @model_validator(mode="after")
    def _check_workflow(self) -> "CampaignConfig":
        if not self.workflow:
            raise ValueError("workflow must define at least one phase")

        for name, phase in self.workflow.items():
            if phase.after is not None and phase.after not in self.workflow:
                raise ValueError(
                    f"phase {name!r} runs after unknown phase {phase.after!r}"
                )

        roots = [n for n, p in self.workflow.items() if p.after is None]
        if len(roots) != 1:
            raise ValueError(
                f"workflow must have exactly one root phase (no `after:`), found {roots}"
            )

        # `after` forms a chain/tree; a cycle shows up as a node that never
        # reaches the root.
        for name in self.workflow:
            seen = {name}
            cursor = self.workflow[name].after
            while cursor is not None:
                if cursor in seen:
                    raise ValueError(f"workflow has a cycle involving phase {name!r}")
                seen.add(cursor)
                cursor = self.workflow[cursor].after

        for metric, cfg in self.key_metrics.items():
            phase = self.workflow.get(cfg.from_phase)
            if phase is None:
                raise ValueError(
                    f"key metric {metric!r} bound to unknown phase {cfg.from_phase!r}"
                )
            if phase.agentic:
                raise ValueError(
                    f"key metric {metric!r} is bound to agentic phase "
                    f"{cfg.from_phase!r}; key metrics must come from a "
                    f"deterministic phase so they can be trusted"
                )
        return self

    # -- ordering ------------------------------------------------------------

    def phase_order(self) -> list[str]:
        """Phases in execution order (root first, each after its predecessor)."""
        by_pred: dict[str | None, list[str]] = {}
        for name, phase in self.workflow.items():
            by_pred.setdefault(phase.after, []).append(name)
        order: list[str] = []
        frontier = list(by_pred.get(None, []))
        while frontier:
            name = frontier.pop(0)
            order.append(name)
            frontier.extend(by_pred.get(name, []))
        return order

    def next_phases(self, phase: str) -> list[str]:
        return [n for n, p in self.workflow.items() if p.after == phase]

    @property
    def root_phase(self) -> str:
        return next(n for n, p in self.workflow.items() if p.after is None)


def load_config(path: str | Path) -> CampaignConfig:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text())
    except Exception as exc:
        raise ConfigError(f"{path}: cannot read config: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: config must be a YAML mapping")
    try:
        return CampaignConfig.model_validate(raw)
    except Exception as exc:
        raise ConfigError(f"{path}: invalid config:\n{exc}") from exc
