"""Shared plan/progress list, mirrored into the UI."""

from __future__ import annotations

from typing import Any

from .base import Tool, ToolContext, ToolResult

STATUSES = ("pending", "in_progress", "completed")


class TodoWriteTool(Tool):
    name = "TodoWrite"
    description = """
Record or update the task list for the current piece of work.

Use it for anything that takes three or more distinct steps, and update it
as you go: mark a task in_progress *before* starting it and completed
*immediately* after finishing it. Exactly one task should be in_progress at
a time. Skip it for single trivial steps.
Always send the full list — it replaces the previous one.
"""

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "description": "Imperative form, e.g. 'Run the tests'"},
                            "activeForm": {"type": "string", "description": "Present continuous, e.g. 'Running the tests'"},
                            "status": {"type": "string", "enum": list(STATUSES)},
                        },
                        "required": ["content", "status"],
                    },
                }
            },
            "required": ["todos"],
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raw = args.get("todos") or []
        if not isinstance(raw, list):
            return ToolResult.error("todos must be a list")

        todos: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status", "pending"))
            if status not in STATUSES:
                status = "pending"
            todos.append(
                {
                    "content": str(item.get("content", "")).strip(),
                    "activeForm": str(item.get("activeForm") or item.get("content", "")).strip(),
                    "status": status,
                }
            )

        in_progress = [t for t in todos if t["status"] == "in_progress"]
        warning = ""
        if len(in_progress) > 1:
            warning = f"\nNote: {len(in_progress)} tasks are in_progress; keep it to one."

        ctx.todos[:] = todos

        done = sum(1 for t in todos if t["status"] == "completed")
        rendered = "\n".join(
            f"  {_glyph(t['status'])} {t['content']}" for t in todos
        )
        return ToolResult(
            content=f"Task list updated ({done}/{len(todos)} done){warning}\n{rendered}",
            summary=f"todos: {done}/{len(todos)} done",
            display={"kind": "todos", "todos": todos},
        )


def _glyph(status: str) -> str:
    return {"completed": "x", "in_progress": ">", "pending": " "}.get(status, " ")
