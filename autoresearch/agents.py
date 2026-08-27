"""The agent-harness adapter.

Agentic phases run on an off-the-shelf agent harness - any SDK or CLI that
gives an agent the loop, filesystem tools, shell, and a skills/prompt
mechanism. The engine only speaks this interface, so a harness is a
per-deployment choice and swapping one changes nothing else (§7).

Three adapters ship:

  ScriptedHarness  - a plain callable; deterministic, used by tests and CI.
  CommandHarness   - runs any harness CLI in the workspace, prompt on stdin.
  sdk_harness()    - an in-process SDK, if one is installed.

Whatever the harness, the engine's expectations are identical: work in the
workspace, and leave a valid `result.json` in the phase directory.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol


class HarnessError(Exception):
    """The harness itself failed (not the work it was asked to do)."""


@dataclass
class AgentRequest:
    """Everything an agentic phase needs, and nothing about the engine."""

    prompt: str
    skills: list[str] = field(default_factory=list)
    workspace: Path = Path(".")
    phase_dir: Path = Path(".")
    #: engine-mediated tools the agent may call, by name
    tools: dict[str, Callable] = field(default_factory=dict)
    timeout_s: float | None = None
    #: paths the agent may read but must not modify (eval harness, launchers)
    read_only: list[Path] = field(default_factory=list)


@dataclass
class AgentResult:
    """What the harness reports back. The engine trusts none of it: the
    phase's actual outcome is read from `result.json` (§7.1)."""

    text: str = ""
    ok: bool = True
    detail: str = ""


class AgentHarness(Protocol):
    def invoke(self, request: AgentRequest) -> AgentResult:
        ...


# -- scripted ----------------------------------------------------------------


class ScriptedHarness:
    """Wraps a plain callable. Keeps agent-free tests exercising the same
    code path as a real harness."""

    def __init__(self, fn: Callable[[AgentRequest], AgentResult | None]):
        self.fn = fn
        self.requests: list[AgentRequest] = []

    def invoke(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        result = self.fn(request)
        return result if isinstance(result, AgentResult) else AgentResult()


# -- command-line harnesses --------------------------------------------------


class CommandHarness:
    """Runs an agent harness CLI with the prompt on stdin, cwd = workspace.

    Deliberately generic: `command` is whatever launches the harness
    non-interactively. Skills are passed through `skill_arg` when the harness
    takes them as flags, and are always named in the prompt regardless.
    """

    def __init__(
        self,
        command: list[str],
        skill_arg: str | None = None,
        env: dict[str, str] | None = None,
    ):
        self.command = list(command)
        self.skill_arg = skill_arg
        self.env = env

    def invoke(self, request: AgentRequest) -> AgentResult:
        argv = list(self.command)
        if self.skill_arg:
            for skill in request.skills:
                argv += [self.skill_arg, skill]

        exe = shutil.which(argv[0])
        if exe is None:
            raise HarnessError(f"agent harness not found on PATH: {argv[0]}")
        argv[0] = exe

        env = dict(os.environ)
        env.update(self.env or {})
        try:
            proc = subprocess.run(
                argv,
                input=request.prompt,
                capture_output=True,
                text=True,
                cwd=str(request.workspace),
                timeout=request.timeout_s,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise HarnessError(
                f"agent harness timed out after {request.timeout_s}s"
            ) from exc
        except OSError as exc:
            raise HarnessError(f"cannot run agent harness: {exc}") from exc

        return AgentResult(
            text=proc.stdout,
            ok=proc.returncode == 0,
            detail=proc.stderr.strip()[-2000:],
        )


def claude_code_harness(extra_args: list[str] | None = None) -> CommandHarness:
    """The Claude Code CLI as a harness: one concrete choice, not a
    requirement - any other harness CLI works the same way."""
    return CommandHarness(
        command=["claude", "-p", *(extra_args or [])],
    )


def sdk_harness():
    """An in-process SDK harness, when one is installed. Imported lazily so
    the engine has no hard dependency on any vendor's package."""
    try:
        from claude_agent_sdk import query  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise HarnessError(
            "no in-process agent SDK installed; use CommandHarness with a "
            "harness CLI, or install one"
        ) from exc

    class _SDKHarness:  # pragma: no cover - requires the SDK
        def invoke(self, request: AgentRequest) -> AgentResult:
            import asyncio

            async def run() -> str:
                chunks: list[str] = []
                async for message in query(
                    prompt=request.prompt,
                    options={"cwd": str(request.workspace)},
                ):
                    chunks.append(str(message))
                return "\n".join(chunks)

            return AgentResult(text=asyncio.run(run()))

    return _SDKHarness()
