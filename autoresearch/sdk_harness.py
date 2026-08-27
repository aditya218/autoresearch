"""An agent harness backed by the Claude Agent SDK.

One concrete implementation of the harness interface (§7). The SDK supplies
the agent loop and its built-in tools - file read/write/edit, bash, search -
which is exactly what an agentic phase needs: work in a workspace, then
report through `result.json`.

Imported lazily by `make_harness` so the engine keeps no hard dependency on
any vendor's package.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from autoresearch.agents import AgentRequest, AgentResult, HarnessError


@dataclass
class ClaudeSDKHarness:
    """Runs an agentic phase in the trial's workspace.

    The guard rails matter as much as the capability: a campaign runs for
    days unattended, so every invocation is bounded in turns, spend, and
    wall-clock, and the agent is confined to the workspace and its own phase
    directory.
    """

    model: str | None = None
    effort: str | None = "high"
    max_turns: int | None = 40
    max_budget_usd: float | None = 5.0
    #: 'acceptEdits' lets the agent edit files without prompting - required
    #: for unattended runs, and safe because it can only reach the dirs below
    permission_mode: str = "acceptEdits"
    system_prompt: str | None = None
    extra_dirs: list[Path] = field(default_factory=list)
    #: populated after each call, for logging and cost accounting
    last_usage: dict = field(default_factory=dict)

    def _options(self, request: AgentRequest):
        from claude_agent_sdk import ClaudeAgentOptions

        # The agent works in the workspace; it may also read its own phase
        # directory and whatever the project marks readable (eval harness,
        # prior results). Everything else is out of reach.
        add_dirs = [str(request.phase_dir)]
        add_dirs += [str(p) for p in request.read_only]
        add_dirs += [str(p) for p in self.extra_dirs]

        options = dict(
            cwd=str(request.workspace),
            add_dirs=add_dirs,
            permission_mode=self.permission_mode,
            max_turns=self.max_turns,
            max_budget_usd=self.max_budget_usd,
        )
        if self.model:
            options["model"] = self.model
        if self.effort:
            options["effort"] = self.effort
        if self.system_prompt:
            options["system_prompt"] = self.system_prompt
        return ClaudeAgentOptions(**options)

    async def _run(self, request: AgentRequest) -> AgentResult:
        try:
            from claude_agent_sdk import query
        except ImportError as exc:
            raise HarnessError(
                "claude-agent-sdk is not installed; `pip install claude-agent-sdk` "
                "or use CommandHarness with a harness CLI"
            ) from exc

        chunks: list[str] = []
        usage: dict = {}
        try:
            async for message in query(prompt=request.prompt, options=self._options(request)):
                kind = type(message).__name__
                if kind == "AssistantMessage":
                    for block in getattr(message, "content", []) or []:
                        text = getattr(block, "text", None)
                        if text:
                            chunks.append(text)
                elif kind == "ResultMessage":
                    usage = {
                        "cost_usd": getattr(message, "total_cost_usd", None),
                        "turns": getattr(message, "num_turns", None),
                        "duration_ms": getattr(message, "duration_ms", None),
                        "is_error": getattr(message, "is_error", False),
                    }
                    result_text = getattr(message, "result", None)
                    if result_text:
                        chunks.append(str(result_text))
        except Exception as exc:  # noqa: BLE001 - any SDK failure is a harness failure
            raise HarnessError(f"agent SDK failed: {exc}") from exc

        self.last_usage = usage
        # An error from the SDK is a harness problem; whether the *work*
        # succeeded is decided by result.json, never by this flag.
        return AgentResult(
            text="\n".join(chunks),
            ok=not usage.get("is_error", False),
            detail=f"turns={usage.get('turns')} cost_usd={usage.get('cost_usd')}",
        )

    def invoke(self, request: AgentRequest) -> AgentResult:
        async def with_timeout():
            if request.timeout_s:
                return await asyncio.wait_for(self._run(request), request.timeout_s)
            return await self._run(request)

        try:
            return asyncio.run(with_timeout())
        except asyncio.TimeoutError as exc:
            raise HarnessError(
                f"agent exceeded its {request.timeout_s}s budget"
            ) from exc


def available() -> bool:
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        return False
    return True
