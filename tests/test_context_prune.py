"""OpenCode-style pruning of older tool outputs."""

from __future__ import annotations

from aiharness.agent.context import PRUNED_TOOL_STUB, prune_old_tool_outputs
from aiharness.providers.base import Message


def _tool(content: str, name: str = "Read", *, pruned: bool = False) -> Message:
    meta: dict = {"tool": name, "is_error": False}
    if pruned:
        meta["pruned"] = True
    return Message(role="tool", content=content, tool_call_id="c1", name=name, meta=meta)


def test_prune_stubs_old_tool_output_when_enough_is_reclaimed():
    bulky = "x" * 80_000
    messages = [
        Message(role="user", content="old"),
        Message(role="assistant", content="ok", tool_calls=[]),
        _tool(bulky),
        Message(role="user", content="mid"),
        _tool("small recent 1"),
        Message(role="user", content="new1"),
        _tool("small recent 2"),
        Message(role="user", content="new2"),
        _tool("keep me"),
    ]
    # Tiny protect / minimum so the fixture stays small.
    pruned = prune_old_tool_outputs(
        messages,
        protect_tokens=5,
        minimum_tokens=100,
        keep_user_turns=2,
    )
    assert pruned >= 1
    assert messages[2].content == PRUNED_TOOL_STUB
    assert messages[2].meta.get("pruned") is True
    assert messages[-1].content == "keep me"


def test_prune_skips_when_reclaim_below_minimum():
    messages = [
        Message(role="user", content="a"),
        _tool("tiny"),
        Message(role="user", content="b"),
        _tool("tiny2"),
        Message(role="user", content="c"),
        _tool("tiny3"),
    ]
    assert (
        prune_old_tool_outputs(
            messages, protect_tokens=1, minimum_tokens=50_000, keep_user_turns=1
        )
        == 0
    )


def test_prune_protects_skill_outputs():
    skill_body = "s" * 80_000
    messages = [
        Message(role="user", content="old"),
        _tool(skill_body, name="Skill"),
        Message(role="user", content="n1"),
        Message(role="user", content="n2"),
        _tool("recent"),
    ]
    assert (
        prune_old_tool_outputs(
            messages, protect_tokens=1, minimum_tokens=10, keep_user_turns=2
        )
        == 0
    )
    assert messages[1].content == skill_body
