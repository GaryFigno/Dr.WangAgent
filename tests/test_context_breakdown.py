"""Attributing the context window to what actually filled it."""

from __future__ import annotations

import pytest

from aiharness.agent.context import ContextSlice, estimate_tokens, measure_context
from aiharness.providers.base import Message
from aiharness.ui.widgets import (
    BREAKDOWN_BAR_WIDTH,
    _compact_tokens,
    _segmented_bar,
    render_context_breakdown,
)


def tool_spec(name: str, size: int = 200) -> dict:
    return {
        "type": "function",
        "function": {"name": name, "description": "d" * size, "parameters": {}},
    }


def make_breakdown(**overrides):
    defaults = {
        "window": 100_000,
        "system_prompt": "prompt " * 500,
        "skills_section": "",
        "messages": [],
        "tool_specs": [],
    }
    defaults.update(overrides)
    return measure_context(**defaults)


# -- attribution -----------------------------------------------------------


def test_each_category_is_counted_separately():
    breakdown = make_breakdown(
        messages=[Message(role="user", content="hello " * 200)],
        tool_specs=[tool_spec("Read"), tool_spec("mcp__github__issue")],
        skills_section="skills " * 100,
    )
    names = {item.name for item in breakdown.slices}
    assert names == {"Messages", "System tools", "MCP tools", "System prompt", "Skills"}


def test_mcp_tools_are_split_from_built_in_ones():
    breakdown = make_breakdown(
        tool_specs=[tool_spec("Read"), tool_spec("Bash"), tool_spec("mcp__github__issue")]
    )
    by_name = {item.name: item for item in breakdown.slices}
    assert by_name["System tools"].tokens > 0
    assert by_name["MCP tools"].tokens > 0
    # Two built-in tools should cost more than one MCP tool of equal size.
    assert by_name["System tools"].tokens > by_name["MCP tools"].tokens


def test_mcp_tools_are_broken_down_per_server():
    breakdown = make_breakdown(
        tool_specs=[
            tool_spec("mcp__github__issue", 400),
            tool_spec("mcp__github__pr", 400),
            tool_spec("mcp__fs__read", 100),
        ]
    )
    detail = next(i for i in breakdown.slices if i.name == "MCP tools").detail
    assert set(detail) == {"github", "fs"}
    assert detail["github"] > detail["fs"]


def test_skills_are_not_double_counted_inside_the_system_prompt():
    """The skills listing lives in the system prompt; counting it twice lies."""
    skills = "skills " * 200
    breakdown = make_breakdown(
        system_prompt="instructions " * 300 + skills, skills_section=skills
    )
    by_name = {item.name: item.tokens for item in breakdown.slices}
    total = by_name["System prompt"] + by_name["Skills"]
    assert total == pytest.approx(
        estimate_tokens("instructions " * 300 + skills), rel=0.02
    )


def test_the_system_message_is_not_counted_as_a_message():
    """It is already counted as the system prompt."""
    with_system = make_breakdown(
        messages=[
            Message(role="system", content="x" * 4000),
            Message(role="user", content="hi"),
        ]
    )
    without = make_breakdown(messages=[Message(role="user", content="hi")])
    assert with_system.used == without.used


def test_empty_categories_are_omitted():
    breakdown = make_breakdown(messages=[], tool_specs=[], skills_section="")
    assert {item.name for item in breakdown.slices} == {"System prompt"}


def test_free_space_is_the_remainder():
    breakdown = make_breakdown(window=50_000)
    assert breakdown.free == 50_000 - breakdown.used
    assert breakdown.fraction == pytest.approx(breakdown.used / 50_000)


def test_free_space_never_goes_negative():
    breakdown = make_breakdown(window=10, messages=[Message(role="user", content="x" * 9000)])
    assert breakdown.free == 0


def test_rows_are_sorted_with_free_space_last():
    breakdown = make_breakdown(
        messages=[Message(role="user", content="m" * 8000)],
        tool_specs=[tool_spec("Read", 50)],
    )
    rows = breakdown.rows()
    tokens = [count for _, count, _ in rows[:-1]]
    assert tokens == sorted(tokens, reverse=True)
    assert rows[-1][0] == "Free space"


def test_largest_slice_is_reported():
    breakdown = make_breakdown(messages=[Message(role="user", content="m" * 20000)])
    assert breakdown.largest().name == "Messages"


# -- rendering -------------------------------------------------------------


def test_the_rendered_panel_lists_every_category():
    breakdown = make_breakdown(
        messages=[Message(role="user", content="hello " * 100)],
        tool_specs=[tool_spec("Read"), tool_spec("mcp__github__issue")],
        skills_section="skills " * 50,
    )
    rendered = str(render_context_breakdown(breakdown))
    for name in ("Messages", "System tools", "MCP tools", "System prompt", "Skills", "Free space"):
        assert name in rendered
    assert "Context window" in rendered
    assert "%" in rendered


def test_the_panel_can_speak_chinese():
    rendered = str(render_context_breakdown(make_breakdown(), chinese=True))
    assert "上下文窗口" in rendered
    assert "剩余" in rendered


def test_per_server_detail_is_rendered():
    breakdown = make_breakdown(
        tool_specs=[tool_spec("mcp__github__issue"), tool_spec("mcp__fs__read")]
    )
    rendered = str(render_context_breakdown(breakdown))
    assert "by server" in rendered
    assert "github" in rendered


def test_the_bar_is_exactly_the_configured_width():
    for window in (10_000, 100_000, 1_000_000):
        breakdown = make_breakdown(
            window=window, messages=[Message(role="user", content="m" * 30000)]
        )
        assert len(str(_segmented_bar(breakdown))) == BREAKDOWN_BAR_WIDTH


def test_the_bar_fills_completely_when_the_window_is_full():
    breakdown = make_breakdown(
        window=1000, messages=[Message(role="user", content="m" * 100000)]
    )
    bar = _segmented_bar(breakdown)
    assert len(str(bar)) == BREAKDOWN_BAR_WIDTH


@pytest.mark.parametrize(
    "value, rendered",
    [(0, "0"), (999, "999"), (1_000, "1.0k"), (673_412, "673.4k"), (1_048_576, "1.0M")],
)
def test_token_counts_are_rendered_readably(value, rendered):
    assert _compact_tokens(value) == rendered


# -- through the agent -----------------------------------------------------


async def test_the_agent_reports_its_own_breakdown(config, router, workspace, sessions):
    from aiharness.agent.loop import Agent
    from aiharness.permissions import PermissionEngine
    from aiharness.toolset import build_registry

    agent = Agent(
        config,
        router,
        build_registry(),
        PermissionEngine(config.permissions, workspace),
        workspace,
        session=sessions.create(workspace),
    )
    agent.add_user_message("a question about the codebase")

    breakdown = agent.context_breakdown()
    assert breakdown.window == agent.context_window()
    assert breakdown.used > 0
    names = {item.name for item in breakdown.slices}
    assert "Messages" in names
    assert "System tools" in names
    await router.aclose()


async def test_the_breakdown_total_tracks_the_status_bar(config, router, workspace, sessions):
    """The panel and the status line must not disagree about the same number."""
    from aiharness.agent.loop import Agent
    from aiharness.permissions import PermissionEngine
    from aiharness.toolset import build_registry

    agent = Agent(
        config,
        router,
        build_registry(),
        PermissionEngine(config.permissions, workspace),
        workspace,
        session=sessions.create(workspace),
    )
    agent.add_user_message("hello " * 200)
    assert agent.context_breakdown().used == pytest.approx(agent.context_used(), rel=0.02)
    await router.aclose()


async def test_the_panel_toggles(config, workspace, sessions):
    from aiharness.ui.app import HarnessApp
    from aiharness.ui.widgets import ContextPanel

    app = HarnessApp(config, workspace)
    async with app.run_test() as pilot:
        await pilot.pause()
        panel = app.query_one(ContextPanel)
        assert not panel.has_class("visible")

        app.action_toggle_context()
        await pilot.pause()
        assert panel.has_class("visible")

        app.action_toggle_context()
        await pilot.pause()
        assert not panel.has_class("visible")


async def test_the_context_command_opens_the_panel(config, workspace, sessions):
    from aiharness.ui.app import HarnessApp
    from aiharness.ui.commands import dispatch
    from aiharness.ui.widgets import ContextPanel

    app = HarnessApp(config, workspace)
    async with app.run_test() as pilot:
        await pilot.pause()
        output = await dispatch(app, "/context")
        assert "Selectable window sizes" in output
        assert app.query_one(ContextPanel).has_class("visible")

        await dispatch(app, "/context hide")
        assert not app.query_one(ContextPanel).has_class("visible")


def test_a_slice_reports_its_share():
    assert ContextSlice("x", 250).share(1000) == pytest.approx(0.25)
    assert ContextSlice("x", 250).share(0) == 0.0
