"""The Orchestrate tool — split, execute, review, repair in one call."""

from __future__ import annotations

from typing import Any

from ..constants import DEFAULT_PARALLEL_AGENTS
from ..providers.router import NoRouteError
from ..workflows.orchestrator import Orchestrator
from .base import Tool, ToolContext, ToolResult


class OrchestrateTool(Tool):
    """Runs a large change end to end across several agents."""

    name = "Orchestrate"
    subagent_safe = False
    bulky = True
    description = """
Run a large piece of work across multiple agents with adversarial review.

Phases: a planner splits the goal into assignments with disjoint file scopes;
workers execute them in parallel (respecting declared dependencies); an
adversarial reviewer reads what was actually written and reports defects; a
repair pass fixes the confirmed ones.

Use for work that is genuinely too big for one pass — a feature touching
several modules, a refactor across many files, a migration. For a single
focused change, edit it yourself: this costs many model calls.

Each phase's model can be pinned separately, including to a specific API
account, e.g. worker_model='ds-chat@deepseek-b'.
"""

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "What to accomplish, stated concretely, with acceptance criteria",
                },
                "worker_model": {
                    "type": "string",
                    "description": "Model for the execution agents: 'model', 'model@account' or 'role:name'",
                },
                "planner_model": {"type": "string", "description": "Model for the split phase"},
                "reviewer_model": {
                    "type": "string",
                    "description": "Model for the adversarial review",
                },
                "parallel": {
                    "type": "integer",
                    "description": f"Concurrent workers (default {DEFAULT_PARALLEL_AGENTS})",
                },
                "review_rounds": {
                    "type": "integer",
                    "description": "Review/repair cycles to run (default 1, 0 disables review)",
                },
                "repair": {
                    "type": "boolean",
                    "description": "Act on review findings (default true)",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Plan and investigate without writing any files",
                },
            },
            "required": ["goal"],
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        from ..toolset import build_registry

        goal = str(args.get("goal", "")).strip()
        if not goal:
            return ToolResult.error("goal is empty")

        orchestrator = Orchestrator(
            ctx,
            build_registry(include_agent_tools=False),
            on_progress=lambda line: None,  # ctx.note is already called inside
        )

        try:
            outcome = await orchestrator.run(
                goal,
                worker_model=args.get("worker_model"),
                planner_model=args.get("planner_model"),
                reviewer_model=args.get("reviewer_model"),
                parallel=int(args.get("parallel") or DEFAULT_PARALLEL_AGENTS),
                review_rounds=int(args.get("review_rounds", 1)),
                repair=args.get("repair", True) is not False,
                read_only=bool(args.get("dry_run")),
            )
        except NoRouteError as error:
            return ToolResult.error(str(error))

        return ToolResult(
            content=outcome.render(),
            is_error=not outcome.ok,
            summary=(
                f"orchestrated {len(outcome.assignments)} assignment(s), "
                f"{len(outcome.phases)} phase(s) — ${outcome.total_cost:.4f}"
            ),
            display={"kind": "orchestrate", "phases": len(outcome.phases)},
        )
