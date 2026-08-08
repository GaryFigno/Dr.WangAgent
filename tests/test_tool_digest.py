"""Harness digests for main-model tool results (not dual-channel model tags)."""

from __future__ import annotations

from aiharness.agent.context import (
    make_prune_digest,
    prepare_tool_result_for_model,
    prune_old_tool_outputs,
    summarize_tool_result,
)
from aiharness.agent.loop import Agent
from aiharness.config.schema import ContextConfig
from aiharness.providers.base import Message, ToolCall
from aiharness.tools.base import ToolResult


def test_bash_success_keeps_tail_and_header():
    body = ("line\n" * 400) + "10 passed in 2.1s\n"
    cfg = ContextConfig(bash_success_chars=200, max_tool_result_chars=12_000)
    wire = prepare_tool_result_for_model(
        "Bash",
        body,
        is_error=False,
        command="pytest -q",
        context=cfg,
    )
    assert wire.startswith("[Bash] exit=0")
    assert "cmd: pytest -q" in wire
    assert "10 passed" in wire
    assert "omitted" in wire
    assert len(wire) < len(body)


def test_bash_failure_keeps_more_tail():
    body = "exit code 2\n\n" + ("noise\n" * 200) + "AssertionError: boom\n"
    cfg = ContextConfig(bash_success_chars=80, bash_error_chars=400)
    wire = prepare_tool_result_for_model(
        "Bash", body, is_error=True, command="pytest tests/x.py", context=cfg
    )
    assert "exit=2" in wire
    assert "AssertionError: boom" in wire
    assert "exit code 2\n\n" not in wire  # redundant prefix stripped


def test_write_edit_pass_through():
    text = "edited /tmp/a.py at line 3 (1 replacement(s))"
    assert prepare_tool_result_for_model("Edit", text) == text


def test_read_respects_read_result_chars():
    body = "x" * 5_000
    cfg = ContextConfig(read_result_chars=500, max_tool_result_chars=12_000)
    wire = prepare_tool_result_for_model("Read", body, context=cfg)
    assert len(wire) < len(body)
    assert "elided by the harness" in wire


def test_prune_digest_keeps_status_not_blank_stub():
    digest = make_prune_digest(
        "Bash",
        "exit code 0\n\n..... 10 passed in 1.0s\n",
        command="pytest -q",
    )
    assert digest.startswith("[digest]")
    assert "exit=0" in digest
    assert "10 passed" in digest
    assert "pytest -q" in digest


def test_prune_uses_structured_digest_and_preserves_full_in_meta():
    bulky = "exit code 0\n\n" + ("ok\n" * 20_000) + "5 passed\n"
    messages = [
        Message(role="user", content="old"),
        Message(
            role="tool",
            content=bulky,
            tool_call_id="c1",
            name="Bash",
            meta={"tool": "Bash", "is_error": False, "command": "pytest -q"},
        ),
        Message(role="user", content="new"),
        Message(
            role="tool",
            content="keep recent",
            tool_call_id="c2",
            name="Read",
            meta={"tool": "Read", "is_error": False},
        ),
    ]
    pruned = prune_old_tool_outputs(
        messages,
        protect_tokens=5,
        minimum_tokens=100,
        keep_user_turns=1,
    )
    assert pruned >= 1
    assert messages[1].meta.get("pruned") is True
    assert messages[1].meta.get("full") == bulky
    assert messages[1].content.startswith("[digest]")
    assert "exit=0" in messages[1].content
    assert "pytest -q" in messages[1].content
    assert messages[-1].content == "keep recent"


def test_summarize_tool_result_for_edit():
    line = summarize_tool_result(
        "Edit", "edited C:/proj/a.py at line 10 (1 replacement(s))"
    )
    assert "Edit" in line
    assert "a.py" in line


def test_agent_record_keeps_full_in_meta_for_audit(agent_parts):
    """Disk/memory meta retains pre-digest text; content is what main sees."""
    config, router, tools, permissions, workspace = agent_parts
    config.context.bash_success_chars = 120
    agent = Agent(config, router, tools, permissions, workspace)
    full = ("pad\n" * 80) + "3 passed\n"
    call = ToolCall(
        id="b1",
        name="Bash",
        arguments='{"command": "pytest -q"}',
    )
    agent._record(call, ToolResult(content=full, summary="ok"))
    stored = agent.messages[-1]
    assert stored.role == "tool"
    assert stored.content.startswith("[Bash] exit=0")
    assert "3 passed" in stored.content
    assert stored.meta.get("full") == full
    assert stored.meta.get("command") == "pytest -q"
    assert len(stored.content) < len(full)
