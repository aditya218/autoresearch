"""Workflow spec parsing and lint. Doc 06.

Declarative DAG, so the engine can schedule, resume, and re-attach without
executing user code to discover what happens next.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

import yaml

LOCAL_CEILING_SECONDS = 20 * 60
LOCAL_CEILING_EXCEPTIONS = {"implement": 60 * 60}   # D26


class SpecError(Exception):
    pass


def parse_duration(s: str | int) -> int:
    if isinstance(s, int):
        return s
    units = {"s": 1, "m": 60, "h": 3600}
    if s[-1] not in units:
        raise SpecError(f"bad duration {s!r}")
    return int(s[:-1]) * units[s[-1]]


@dataclass
class Stage:
    key: str
    kind: str                       # 'local' | 'external_job'
    needs: list[str] = field(default_factory=list)
    timeout: int = 600
    command: list[str] | None = None            # local
    launch: str | None = None                   # external_job
    poll: str | None = None
    find: str | None = None                     # optional (D11)
    logs: str | None = None
    poll_interval: int = 5
    failure_class: str | None = None            # forces classification
    status_map: dict[str, str] = field(default_factory=dict)
    max_infra_retries: int = 3
    outputs: dict[str, str] = field(default_factory=dict)


@dataclass
class Workflow:
    name: str
    version: int
    stages: dict[str, Stage]
    order: list[str]
    terminal: list[str]
    raw: dict

    @property
    def workflow_version(self) -> str:
        canonical = json.dumps(self.raw, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()[:32]

    def recovery_tier(self) -> str:
        """Which launch-recovery tier this workflow achieves (D11)."""
        ext = [s for s in self.stages.values() if s.kind == "external_job"]
        if not ext:
            return "n/a"
        if all(s.find for s in ext):
            return "find"
        return "receipt"        # the engine always writes/reads a receipt


def load(path: str) -> Workflow:
    with open(path) as fh:
        raw = yaml.safe_load(fh)
    return build(raw)


def build(raw: dict) -> Workflow:
    if not raw.get("name"):
        raise SpecError("workflow needs a name")
    stages: dict[str, Stage] = {}

    for item in raw.get("stages", []):
        key = item.get("key")
        if not key:
            raise SpecError("every stage needs a key")
        if key in stages:
            raise SpecError(f"duplicate stage key {key!r}")
        kind = item.get("kind")
        if kind not in ("local", "external_job"):
            raise SpecError(f"{key}: kind must be local or external_job")

        st = Stage(
            key=key,
            kind=kind,
            needs=item.get("needs", []),
            timeout=parse_duration(item.get("timeout", "10m")),
            command=item.get("command"),
            launch=item.get("launch"),
            poll=item.get("poll"),
            find=item.get("find"),
            logs=item.get("logs"),
            poll_interval=parse_duration(item.get("poll_interval", "5s")),
            failure_class=item.get("failure_class"),
            status_map=item.get("status_map", {}),
            max_infra_retries=item.get("max_infra_retries", 3),
            outputs=item.get("outputs", {}),
        )

        # Lint rule 2: local stages must be cheap, because a controller crash
        # re-executes them from scratch. This is what makes the durability
        # guarantee real rather than aspirational.
        if st.kind == "local":
            ceiling = LOCAL_CEILING_EXCEPTIONS.get(key, LOCAL_CEILING_SECONDS)
            if st.timeout > ceiling:
                raise SpecError(
                    f"{key}: local stage timeout {st.timeout}s exceeds ceiling {ceiling}s "
                    f"— make it an external_job"
                )
            if not st.command:
                raise SpecError(f"{key}: local stage needs a command")

        # Lint rule 3: launch and poll are required; find is optional (D11).
        if st.kind == "external_job":
            if not st.launch or not st.poll:
                raise SpecError(f"{key}: external_job needs launch and poll")

        stages[key] = st

    for st in stages.values():
        for dep in st.needs:
            if dep not in stages:
                raise SpecError(f"{st.key}: unknown dependency {dep!r}")

    order = toposort(stages)        # lint rule 1: DAG, no cycles

    terminal = raw.get("terminal") or [order[-1]]
    for t in terminal:
        if t not in stages:
            raise SpecError(f"terminal stage {t!r} not defined")

    metric_stages = [s.key for s in stages.values() if "metrics" in s.outputs]
    if len(metric_stages) != 1:
        raise SpecError(
            f"exactly one stage must declare an outputs.metrics path, found {metric_stages}"
        )

    return Workflow(
        name=raw["name"], version=raw.get("version", 1),
        stages=stages, order=order, terminal=terminal, raw=raw,
    )


def toposort(stages: dict[str, Stage]) -> list[str]:
    visited: dict[str, int] = {}
    order: list[str] = []

    def visit(key: str, path: tuple[str, ...]) -> None:
        state = visited.get(key, 0)
        if state == 1:
            raise SpecError(f"cycle in workflow: {' -> '.join(path + (key,))}")
        if state == 2:
            return
        visited[key] = 1
        for dep in stages[key].needs:
            visit(dep, path + (key,))
        visited[key] = 2
        order.append(key)

    for key in stages:
        visit(key, ())
    return order
