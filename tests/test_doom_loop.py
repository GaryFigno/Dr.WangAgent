"""Refuse identical tool calls that loop without progress."""

from __future__ import annotations

import pytest

from aiharness.agent.loop import Agent
from aiharness.constants import DOOM_LOOP_THRESHOLD
from aiharness.providers.base import ToolCall


@pytest.mark.asyncio
async def test_doom_loop_refuses_identical_tool_repeats(agent_parts, sessions):
    config, router, tools, permissions, workspace = agent_parts
    session = sessions.create(workspace)
    agent = Agent(config, router, tools, permissions, workspace, session=session)
    call = ToolCall(id="c1", name="Read", arguments='{"file_path":"hello.txt"}')
    for _ in range(DOOM_LOOP_THRESHOLD):
        result = await agent._invoke(call)
        assert not result.is_error, result.content
    blocked = await agent._invoke(call)
    assert blocked.is_error and "doom-loop" in blocked.content
    await router.aclose()


@pytest.mark.asyncio
async def test_doom_loop_resets_on_new_user_message(agent_parts, sessions):
    config, router, tools, permissions, workspace = agent_parts
    session = sessions.create(workspace)
    agent = Agent(config, router, tools, permissions, workspace, session=session)
    call = ToolCall(id="c1", name="Read", arguments='{"file_path":"hello.txt"}')
    for _ in range(DOOM_LOOP_THRESHOLD):
        result = await agent._invoke(call)
        assert not result.is_error
    blocked = await agent._invoke(call)
    assert blocked.is_error and "doom-loop" in blocked.content
    agent.add_user_message("try again differently")
    ok = await agent._invoke(call)
    assert not ok.is_error
    await router.aclose()
