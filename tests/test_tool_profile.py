"""Lite tool profile hides multi-agent tools (T30)."""

from __future__ import annotations

from aiharness.constants import LITE_EXCLUDED_TOOLS


def test_lite_profile_excludes_orchestration_tools(agent_parts, sessions):
    config, router, tools, permissions, workspace = agent_parts
    from aiharness.agent.loop import Agent

    agent = Agent(
        config, router, tools, permissions, workspace, session=sessions.create(workspace)
    )
    full_names = {
        (spec.get("function") or {}).get("name") for spec in agent._tool_specs()
    }
    agent.set_tool_profile("lite")
    lite_names = {
        (spec.get("function") or {}).get("name") for spec in agent._tool_specs()
    }
    assert "Read" in lite_names
    for name in LITE_EXCLUDED_TOOLS:
        if name in full_names:
            assert name not in lite_names
