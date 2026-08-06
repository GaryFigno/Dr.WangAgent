"""The Skill tool — progressive disclosure of packaged instructions."""

from __future__ import annotations

from typing import Any

from .base import Tool, ToolContext, ToolResult


class SkillTool(Tool):
    name = "Skill"
    bulky = True
    description = """
Load a skill's full instructions.

Skills are packaged procedures for particular kinds of work; the system
prompt lists each one's name and description. When a task matches a listed
skill, call this FIRST — the instructions load into the conversation and you
follow them instead of your default approach. Use the exact name from the
listing; do not guess names.
"""

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Exact skill name from the listing"},
                "args": {"type": "string", "description": "Optional arguments to pass through"},
            },
            "required": ["name"],
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.skills is None:
            return ToolResult.error("skills are not enabled in this session")

        name = str(args.get("name", "")).strip()
        skill = ctx.skills.get(name)
        if skill is None:
            available = ", ".join(ctx.skills.names()) or "(none installed)"
            return ToolResult.error(f"No skill named '{name}'. Available: {available}")

        body = skill.render()
        passthrough = str(args.get("args") or "").strip()
        if passthrough:
            body += f"\n\n---\nArguments supplied with this invocation: {passthrough}"

        if skill.allowed_tools:
            body += (
                "\n\n---\nThis skill declares it needs only these tools: "
                + ", ".join(skill.allowed_tools)
            )

        return ToolResult(
            content=body,
            summary=f"loaded skill '{skill.name}'",
            display={"kind": "skill", "name": skill.name, "path": str(skill.path)},
        )


class ListSkillsTool(Tool):
    name = "ListSkills"
    description = "List installed skills with their descriptions and source paths."

    def schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.skills is None or not ctx.skills.all():
            return ToolResult(content="No skills installed.", summary="0 skills")
        lines = [
            f"- {s.name} [{s.source}] — {' '.join(s.description.split())[:200]}\n  {s.path}"
            for s in ctx.skills.all()
        ]
        return ToolResult(
            content="\n".join(lines), summary=f"{len(lines)} skill(s)"
        )
