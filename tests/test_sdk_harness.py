"""The Claude Agent SDK harness.

Tested without calling the API: the SDK's `query` is stubbed so these run
offline and for free, which is what CI needs. What they pin is the wiring -
that the agent is pointed at the workspace, confined to the right
directories, bounded in turns and spend, and that its chatter never decides
the phase's outcome.

An opt-in live test at the bottom makes a real call when explicitly asked.
"""

import os
import sys
import types
from pathlib import Path

import pytest

from autoresearch.agents import AgentRequest, HarnessError
from autoresearch.sdk_harness import ClaudeSDKHarness, available


class FakeMessage:
    """Stands in for the SDK's message classes, matched on class name."""

    def __init__(self, name, **fields):
        self.__class__ = type(name, (FakeMessage,), {})
        self.__dict__.update(fields)


def install_fake_sdk(monkeypatch, messages, capture=None, raises=None):
    """Replace claude_agent_sdk with a stub for the duration of a test."""

    def options_factory(**kwargs):
        if capture is not None:
            capture.update(kwargs)
        return types.SimpleNamespace(**kwargs)

    async def fake_query(*, prompt, options=None, **_):
        if capture is not None:
            capture["prompt"] = prompt
        if raises is not None:
            raise raises
        for message in messages:
            yield message

    module = types.ModuleType("claude_agent_sdk")
    module.query = fake_query
    module.ClaudeAgentOptions = options_factory
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", module)
    return module


def assistant(text):
    msg = FakeMessage("AssistantMessage")
    msg.content = [types.SimpleNamespace(text=text)]
    return msg


def result(cost=0.02, turns=3, is_error=False, text="done"):
    msg = FakeMessage("ResultMessage")
    msg.total_cost_usd = cost
    msg.num_turns = turns
    msg.duration_ms = 1200
    msg.is_error = is_error
    msg.result = text
    return msg


def request(tmp_path, **kw):
    ws = tmp_path / "ws"
    phase = tmp_path / "phase"
    ws.mkdir(exist_ok=True)
    phase.mkdir(exist_ok=True)
    return AgentRequest(
        prompt=kw.pop("prompt", "do the thing"),
        workspace=ws, phase_dir=phase, **kw,
    )


# -- wiring ------------------------------------------------------------------


def test_agent_runs_in_the_workspace(tmp_path, monkeypatch):
    capture = {}
    install_fake_sdk(monkeypatch, [assistant("hi"), result()], capture)
    req = request(tmp_path)
    ClaudeSDKHarness().invoke(req)
    assert capture["cwd"] == str(req.workspace)


def test_agent_may_reach_phase_dir_and_read_only_paths(tmp_path, monkeypatch):
    """The eval harness must be readable, or analysis can't do its job - and
    it lives outside the workspace precisely so it can't be rewritten."""
    capture = {}
    install_fake_sdk(monkeypatch, [result()], capture)
    eval_dir = tmp_path / "project"
    eval_dir.mkdir()
    req = request(tmp_path, read_only=[eval_dir])
    ClaudeSDKHarness().invoke(req)
    assert str(req.phase_dir) in capture["add_dirs"]
    assert str(eval_dir) in capture["add_dirs"]
    assert str(req.workspace) not in capture["add_dirs"]  # it's the cwd


def test_unattended_runs_are_bounded(tmp_path, monkeypatch):
    """A campaign runs for days: every invocation is capped in turns, spend,
    and permission scope."""
    capture = {}
    install_fake_sdk(monkeypatch, [result()], capture)
    ClaudeSDKHarness(max_turns=12, max_budget_usd=2.5).invoke(request(tmp_path))
    assert capture["max_turns"] == 12
    assert capture["max_budget_usd"] == 2.5
    assert capture["permission_mode"] == "acceptEdits"


def test_model_and_effort_are_passed_when_set(tmp_path, monkeypatch):
    capture = {}
    install_fake_sdk(monkeypatch, [result()], capture)
    ClaudeSDKHarness(model="claude-opus-5", effort="xhigh").invoke(request(tmp_path))
    assert capture["model"] == "claude-opus-5"
    assert capture["effort"] == "xhigh"


def test_prompt_reaches_the_sdk(tmp_path, monkeypatch):
    capture = {}
    install_fake_sdk(monkeypatch, [result()], capture)
    ClaudeSDKHarness().invoke(request(tmp_path, prompt="specific instructions"))
    assert capture["prompt"] == "specific instructions"


# -- results -----------------------------------------------------------------


def test_assistant_text_and_usage_are_collected(tmp_path, monkeypatch):
    install_fake_sdk(
        monkeypatch, [assistant("first"), assistant("second"), result(cost=0.31, turns=7)]
    )
    harness = ClaudeSDKHarness()
    out = harness.invoke(request(tmp_path))
    assert "first" in out.text and "second" in out.text
    assert out.ok is True
    assert harness.last_usage["cost_usd"] == 0.31
    assert harness.last_usage["turns"] == 7


def test_sdk_error_flag_is_reported_not_raised(tmp_path, monkeypatch):
    """`ok` describes the harness, not the work: the phase's outcome still
    comes from result.json."""
    install_fake_sdk(monkeypatch, [result(is_error=True)])
    assert ClaudeSDKHarness().invoke(request(tmp_path)).ok is False


def test_sdk_exception_becomes_a_harness_error(tmp_path, monkeypatch):
    install_fake_sdk(monkeypatch, [], raises=RuntimeError("connection lost"))
    with pytest.raises(HarnessError, match="connection lost"):
        ClaudeSDKHarness().invoke(request(tmp_path))


def test_missing_sdk_says_what_to_do(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    with pytest.raises(HarnessError, match="claude-agent-sdk is not installed"):
        ClaudeSDKHarness().invoke(request(tmp_path))


def test_timeout_is_enforced(tmp_path, monkeypatch):
    import asyncio

    async def slow_query(*, prompt, options=None, **_):
        await asyncio.sleep(5)
        yield result()

    module = types.ModuleType("claude_agent_sdk")
    module.query = slow_query
    module.ClaudeAgentOptions = lambda **kw: types.SimpleNamespace(**kw)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", module)

    with pytest.raises(HarnessError, match="budget"):
        ClaudeSDKHarness().invoke(request(tmp_path, timeout_s=0.2))


# -- the real thing (opt in) -------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("AUTORESEARCH_LIVE_SDK") or not available(),
    reason="set AUTORESEARCH_LIVE_SDK=1 to make a real API call",
)
def test_live_agent_edits_the_workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "config.json").write_text('{"momentum": 0.0}\n')
    phase = tmp_path / "phase"
    phase.mkdir()

    harness = ClaudeSDKHarness(max_turns=8, max_budget_usd=1.0, effort="low")
    harness.invoke(
        AgentRequest(
            prompt=(
                'Set "momentum" to 0.9 in config.json in the current directory, '
                f'then write result.json into {phase} containing exactly: '
                '{"status": "passed", "metrics": {}, "notes": "set momentum", '
                '"artifacts": []}'
            ),
            workspace=ws, phase_dir=phase, timeout_s=300,
        )
    )
    assert '"momentum": 0.9' in (ws / "config.json").read_text()
    assert (phase / "result.json").exists()
