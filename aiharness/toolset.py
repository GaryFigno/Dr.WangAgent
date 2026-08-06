"""Assembly of the tool registry.

Kept out of :mod:`aiharness.tools` so that importing a single tool module does
not drag in the agent loop — the workflow tools depend on the agent, and the
agent depends on the tool base classes.
"""

from __future__ import annotations

from .tools.base import Tool, ToolRegistry
from .tools.fs import EditTool, GlobTool, GrepTool, ReadTool, WriteTool
from .tools.shell import BashTool
from .tools.skill_tool import ListSkillsTool, SkillTool
from .tools.todo import TodoWriteTool


def core_tools() -> list[Tool]:
    """Tools every agent gets, including subagents."""
    return [
        ReadTool(),
        WriteTool(),
        EditTool(),
        GlobTool(),
        GrepTool(),
        BashTool(),
        TodoWriteTool(),
        SkillTool(),
        ListSkillsTool(),
    ]


def interaction_tools() -> list[Tool]:
    """Tools that hand control back to the user. Never given to subagents."""
    from .tools.interaction import AskUserTool, PresentPlanTool

    return [AskUserTool(), PresentPlanTool()]


def agent_tools() -> list[Tool]:
    """Tools that spawn other models. Never granted to subagents."""
    from .tools.agents import DelegateTool, ResearchTool, TaskTool
    from .tools.orchestrate import OrchestrateTool
    from .tools.workflows import ChallengeTool, VerifyTool

    return [
        DelegateTool(),
        TaskTool(),
        ResearchTool(),
        ChallengeTool(),
        VerifyTool(),
        OrchestrateTool(),
    ]


def build_registry(
    *,
    include_agent_tools: bool = True,
    include_desktop: bool = False,
    include_browser: bool = False,
    include_market: bool = False,
    extra_tools: list[Tool] | None = None,
) -> ToolRegistry:
    """Build a registry.

    Args:
      include_agent_tools: Whether to expose the model-spawning and
        user-facing tools. Set ``False`` for a subagent's registry, so agents
        cannot recurse indefinitely or try to prompt a user who is not there.
      include_desktop: Whether to expose screen, mouse and keyboard control.
        Only ever true when ``desktop.enabled`` is set in config.
      include_browser: Whether to expose the bundled Playwright browser.
        Only ever true when ``browser.enabled`` is set in config.
      include_market: Whether to expose price charts, screening and the paper
        account. Only ever true when ``market.enabled`` is set in config.
      extra_tools: Additional tools, typically MCP proxies.

    Returns:
      A populated :class:`~aiharness.tools.base.ToolRegistry`.
    """
    tools = core_tools()
    if include_agent_tools:
        from .tools.team import lead_tools

        tools += agent_tools()
        tools += interaction_tools()
        tools += lead_tools()
    if include_desktop:
        from .tools.computer import desktop_tools

        tools += desktop_tools()
    if include_browser:
        from .tools.browser import browser_tools

        tools += browser_tools()
    if include_market:
        from .tools.market import market_tools

        tools += market_tools()
    if extra_tools:
        tools += extra_tools
    return ToolRegistry(tools)
