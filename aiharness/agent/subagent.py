"""Running subagents — sequentially or fanned out in parallel.

Subagents get their own transcript and their own model/account selection, so
a cheap model can grind through mechanical work while an expensive one keeps
the conversation, and several models can investigate the same question at
once without sharing context.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..providers.base import ProviderError, Usage
from ..providers.router import NoRouteError, Selection
from ..tools.base import ToolContext, ToolRegistry
from .loop import Agent, Done, Notice, Text, ToolEnd, ToolStart
from .prompts import SUBAGENT_PROMPT

# Subagents never get tools that would let them spawn more agents.
NESTING_TOOLS = {
    "Task", "Research", "Delegate", "Challenge", "Verify", "Orchestrate",
    "SpawnAgent", "AskUser", "PresentPlan",
}


@dataclass
class SubagentSpec:
    prompt: str
    selection: Selection
    label: str = ""
    system_prompt: str = ""
    max_turns: int = 20
    tool_names: list[str] | None = None  # None = every subagent-safe tool


@dataclass
class SubagentResult:
    label: str
    selection: str
    text: str
    usage: Usage = field(default_factory=Usage)
    cost: float = 0.0
    turns: int = 0
    tool_calls: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


ProgressHook = Callable[[str, str], None]  # (label, line)


async def run_subagent(
    spec: SubagentSpec,
    parent_ctx: ToolContext,
    tools: ToolRegistry,
    *,
    on_progress: ProgressHook | None = None,
) -> SubagentResult:
    cfg = parent_ctx.config
    label = spec.label or spec.selection.model_id

    if parent_ctx.depth >= parent_ctx.max_depth:
        return SubagentResult(
            label=label,
            selection=spec.selection.label(),
            text="",
            error=f"subagent depth limit ({parent_ctx.max_depth}) reached",
        )

    names = spec.tool_names or [n for n in tools.names() if n not in NESTING_TOOLS]
    sub_tools = tools.subset([n for n in names if n not in NESTING_TOOLS])

    child_ctx = ToolContext(
        workspace=parent_ctx.workspace,
        config=cfg,
        permissions=parent_ctx.permissions,
        router=parent_ctx.router,
        skills=parent_ctx.skills,
        approve=parent_ctx.approve,
        progress=(lambda line: on_progress(label, line)) if on_progress else None,
        read_files=dict(parent_ctx.read_files),
        todos=[],
        depth=parent_ctx.depth + 1,
        max_depth=parent_ctx.max_depth,
        # The mesh is shared so teammates can reach each other; the identity
        # is whichever one this subagent is playing.
        mesh=parent_ctx.mesh,
        identity=(parent_ctx.mesh.resolve(label) if parent_ctx.mesh else None),
    )

    agent = Agent(
        cfg,
        parent_ctx.router,
        sub_tools,
        parent_ctx.permissions,
        parent_ctx.workspace,
        skills=parent_ctx.skills,
        selection=spec.selection,
        system_prompt=spec.system_prompt or _subagent_system(parent_ctx, spec),
        tool_context=child_ctx,
        subagent=True,
    )
    agent.config = cfg
    original_max = cfg.max_agent_turns
    text_parts: list[str] = []
    tool_calls = 0
    error = ""

    try:
        # Constrain only this agent's turn budget.
        agent.config = _shallow_override(cfg, spec.max_turns)
        async for event in agent.run(spec.prompt):
            if isinstance(event, Text):
                text_parts.append(event.text)
            elif isinstance(event, ToolStart):
                tool_calls += 1
                if on_progress:
                    on_progress(label, f"{event.name}")
            elif isinstance(event, ToolEnd):
                if on_progress and event.result.summary:
                    on_progress(label, f"{event.name}: {event.result.summary}")
            elif isinstance(event, Notice) and event.level == "error":
                error = event.text
            elif isinstance(event, Done):
                if event.text:
                    text_parts = [event.text]
                if event.interrupted:
                    error = error or "interrupted"
    except (ProviderError, NoRouteError) as e:
        error = str(e)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"
    finally:
        cfg.max_agent_turns = original_max

    # Files the subagent read count as read for the parent too.
    parent_ctx.read_files.update(child_ctx.read_files)

    return SubagentResult(
        label=label,
        selection=spec.selection.label(),
        text="".join(text_parts).strip(),
        usage=agent.state.total_usage,
        cost=agent.state.total_cost,
        turns=agent.state.turns,
        tool_calls=tool_calls,
        error=error,
    )


def _shallow_override(cfg: Any, max_turns: int) -> Any:
    """Return cfg with max_agent_turns swapped, sharing everything else.

    The router, ledger and permission engine are deliberately shared so cost
    accounting and approvals stay unified across parent and children.
    """
    import copy

    clone = copy.copy(cfg)
    clone.max_agent_turns = max_turns
    return clone


def _subagent_system(parent_ctx: ToolContext, spec: SubagentSpec) -> str:
    from ..tools.shell import find_shell
    from .prompts import build_system_prompt

    _, _, dialect = find_shell()
    return build_system_prompt(
        parent_ctx.workspace,
        shell=dialect,
        skills_section=parent_ctx.skills.prompt_section() if parent_ctx.skills else "",
        extra=SUBAGENT_PROMPT,
        permission_mode=parent_ctx.permissions.mode,
    )


async def run_parallel(
    specs: list[SubagentSpec],
    parent_ctx: ToolContext,
    tools: ToolRegistry,
    *,
    limit: int = 4,
    on_progress: ProgressHook | None = None,
) -> list[SubagentResult]:
    """Run subagents concurrently, capped at ``limit`` in flight."""
    semaphore = asyncio.Semaphore(max(limit, 1))

    async def guarded(spec: SubagentSpec) -> SubagentResult:
        async with semaphore:
            return await run_subagent(spec, parent_ctx, tools, on_progress=on_progress)

    results = await asyncio.gather(*(guarded(s) for s in specs), return_exceptions=True)
    out: list[SubagentResult] = []
    for spec, outcome in zip(specs, results, strict=True):
        if isinstance(outcome, SubagentResult):
            out.append(outcome)
        else:
            out.append(
                SubagentResult(
                    label=spec.label or spec.selection.model_id,
                    selection=spec.selection.label(),
                    text="",
                    error=f"{type(outcome).__name__}: {outcome}",
                )
            )
    return out
