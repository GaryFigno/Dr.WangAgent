"""Tools for running a team of agents on one project.

These differ from ``Delegate`` and ``Task`` in one way that matters: the
agents they create keep an identity and a mailbox, so they can tell each
other things while the work is in flight. That is worth the extra machinery
only when the parts genuinely interact — an API and its callers, a schema and
its migrations. For independent work, ``Delegate`` is cheaper and less prone
to agents talking in circles.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..agent.mesh import DEFAULT_TEAM, AgentMesh, MeshError, MessageKind
from ..agent.subagent import SubagentSpec, run_subagent
from ..constants import MESSAGE_REPLY_TIMEOUT, TASK_MAX_TURNS
from ..providers.router import NoRouteError, Selection
from .base import Tool, ToolContext, ToolResult

#: Tools a team member gets. No spawning: only the lead assembles the team.
MEMBER_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash", "Skill", "SendMessage", "Inbox"]
MEMBER_TOOLS_READ_ONLY = ["Read", "Glob", "Grep", "Bash", "Skill", "SendMessage", "Inbox"]


def _require_mesh(ctx: ToolContext) -> AgentMesh | None:
    if ctx.mesh is None:
        ctx.mesh = AgentMesh()
    return ctx.mesh


def _me(ctx: ToolContext) -> str:
    return getattr(ctx.identity, "id", "lead")


class SpawnAgentTool(Tool):
    """Creates a teammate with its own identity, session and mailbox."""

    name = "SpawnAgent"
    subagent_safe = False
    bulky = True
    description = f"""
Create a teammate that works alongside you and can message you.

Each teammate gets its own persisted session — visible in the session list,
so the user can read what it did — plus a mailbox, and optionally its own
model and API account.

Give it `owns`: the files it may edit. Two agents editing one file overwrite
each other, so ownership is checked and overlaps are refused.

Use this only when the work genuinely interacts. For independent chunks,
`Delegate` costs less and coordinates better by not coordinating at all.

`background: true` returns immediately and delivers the report to your inbox
when it finishes, so you can run several at once. Suggested roles:
{', '.join(member.role for member in DEFAULT_TEAM)}.
"""

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "role": {"type": "string", "description": "Short name, e.g. 'api' or 'reviewer'"},
                "brief": {
                    "type": "string",
                    "description": "Complete, self-contained instructions; it cannot see this conversation",
                },
                "model": {
                    "type": "string",
                    "description": "'model', 'model@account' or 'role:name'. Defaults to the main model.",
                },
                "owns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Files this teammate may edit",
                },
                "read_only": {"type": "boolean"},
                "background": {"type": "boolean", "description": "Return immediately"},
                "max_turns": {"type": "integer"},
            },
            "required": ["role", "brief"],
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        from ..toolset import build_registry

        mesh = _require_mesh(ctx)
        role = str(args.get("role", "")).strip()
        brief = str(args.get("brief", "")).strip()
        if not role or not brief:
            return ToolResult.error("both role and brief are required")

        owns = [str(f) for f in (args.get("owns") or [])]
        clashes = mesh.conflicts(_me(ctx), owns)
        if clashes:
            return ToolResult.error(
                f"these files are already owned by another agent: {', '.join(clashes)}. "
                "Split the work differently or hand ownership over first."
            )

        try:
            identity = mesh.register(
                role, brief, model=str(args.get("model") or ""), owns=owns, parent=_me(ctx)
            )
        except MeshError as error:
            return ToolResult.error(str(error))

        try:
            selection = (
                Selection.parse(identity.model, ctx.config)
                if identity.model
                else Selection.from_binding(ctx.config.role("main"))
            )
        except (NoRouteError, AttributeError) as error:
            mesh.retire(identity.id)
            return ToolResult.error(f"cannot resolve a model for '{role}': {error}")

        if ctx.make_session is not None:
            handle = ctx.make_session(f"[{identity.role}] {brief[:40]}")
            identity.session_id = getattr(getattr(handle, "meta", None), "id", "")

        spec = SubagentSpec(
            prompt=_member_prompt(identity, mesh, brief),
            selection=selection,
            label=identity.role,
            max_turns=int(args.get("max_turns") or TASK_MAX_TURNS),
            tool_names=MEMBER_TOOLS_READ_ONLY if args.get("read_only") else MEMBER_TOOLS,
        )
        registry = build_registry(include_agent_tools=False, extra_tools=team_tools())

        if args.get("background"):
            asyncio.create_task(
                _run_member(spec, ctx, registry, mesh, identity, _me(ctx)),
                name=f"aih-team-{identity.role}",
            )
            return ToolResult(
                content=(
                    f"Spawned **{identity.role}** (`{identity.id}`) on {selection.label()}, "
                    f"running in the background. Its report will arrive in your inbox — "
                    f"call `Inbox` to collect it."
                ),
                summary=f"spawned {identity.role} (background)",
            )

        identity.busy = True
        ctx.note(f"team: {identity.role} on {selection.label()}")
        result = await run_subagent(spec, ctx, registry, on_progress=lambda label, line: ctx.note(f"[{label}] {line}"))
        identity.busy = False

        if not result.ok and not result.text:
            return ToolResult.error(f"{identity.role} failed: {result.error}")
        return ToolResult(
            content=f"### {identity.role} ({selection.label()})\n\n{result.text}",
            summary=f"{identity.role} finished — ${result.cost:.4f}",
            display={"kind": "team", "role": identity.role},
        )


async def _run_member(spec, ctx, registry, mesh, identity, parent_id: str) -> None:
    """Run a background teammate and post its report to the parent's inbox."""
    identity.busy = True
    try:
        result = await run_subagent(spec, ctx, registry)
        body = result.text or f"(no output; error: {result.error})"
        mesh.send(
            identity.id,
            parent_id,
            f"Finished my brief.\n\n{body}",
            kind=MessageKind.REPLY,
        )
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001 - a failed member must not kill the lead
        mesh.send(identity.id, parent_id, f"I failed: {error}", kind=MessageKind.REPLY)
    finally:
        identity.busy = False


def _member_prompt(identity, mesh: AgentMesh, brief: str) -> str:
    """The brief handed to a teammate, plus who else is on the job."""
    roster = mesh.summary()
    owned = ", ".join(identity.owns) if identity.owns else "(none assigned)"
    return (
        f"You are **{identity.role}** on a team working one codebase.\n\n"
        f"Your brief:\n{brief}\n\n"
        f"Files you own and may edit: {owned}\n"
        f"Do not edit files owned by anyone else. If you need a change in "
        f"someone else's file, use `SendMessage` to ask them.\n\n"
        f"The team:\n{roster}\n\n"
        f"Tell teammates about anything that changes their work — a signature "
        f"you altered, an assumption you broke. Check `Inbox` before you start "
        f"and again before you finish."
    )


class SendMessageTool(Tool):
    name = "SendMessage"
    description = """
Send a message to another agent on the team.

Say what changed and what they need to do about it — a message that is only
a status update wastes their turn. Set `wait` when you genuinely cannot
continue without their answer; otherwise send and carry on.
"""

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Role name or agent id"},
                "content": {"type": "string"},
                "kind": {"type": "string", "enum": ["info", "request", "reply", "handoff"]},
                "wait": {"type": "boolean", "description": "Block until they answer"},
            },
            "required": ["to", "content"],
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        mesh = _require_mesh(ctx)
        try:
            kind = MessageKind(str(args.get("kind") or "info"))
        except ValueError:
            kind = MessageKind.INFO

        try:
            message = mesh.send(_me(ctx), str(args["to"]), str(args.get("content", "")), kind=kind)
        except MeshError as error:
            return ToolResult.error(str(error))

        target = mesh.resolve(str(args["to"]))
        ctx.note(f"→ {target.role if target else args['to']}")

        if not args.get("wait"):
            return ToolResult(
                content=f"Delivered to {target.role if target else args['to']}.",
                summary=f"message → {target.role if target else args['to']}",
            )

        mailbox = mesh.mailbox(_me(ctx))
        if mailbox is None:
            return ToolResult.error("you have no mailbox, so no reply can reach you")
        reply = await mailbox.wait_for_reply(message.id, timeout=MESSAGE_REPLY_TIMEOUT)
        if reply is None:
            return ToolResult.error(
                f"no reply within {MESSAGE_REPLY_TIMEOUT:.0f}s. Continue without it "
                f"and say what you assumed."
            )
        return ToolResult(content=reply.render(), summary=f"reply from {reply.sender}")


class InboxTool(Tool):
    name = "Inbox"
    description = """
Read and clear the messages other agents have sent you.

Check this before starting and before finishing: a teammate may have changed
something your work depends on.
"""

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "peek": {"type": "boolean", "description": "Read without clearing"}
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        mesh = _require_mesh(ctx)
        mailbox = mesh.mailbox(_me(ctx))
        if mailbox is None:
            return ToolResult(content="You have no mailbox.", summary="no mailbox")

        messages = mailbox.peek() if args.get("peek") else mailbox.drain()
        if not messages:
            return ToolResult(content="No new messages.", summary="inbox empty")

        note = ""
        if mailbox.dropped:
            note = f"\n\n[{mailbox.dropped} older message(s) were dropped — the inbox is bounded]"
        body = "\n\n---\n\n".join(message.render() for message in messages)
        return ToolResult(
            content=body + note, summary=f"{len(messages)} message(s)"
        )


class TeamTool(Tool):
    name = "Team"
    description = "List the agents on this job, what they own, and who has unread messages."

    def schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        mesh = _require_mesh(ctx)
        return ToolResult(content=mesh.summary(), summary=f"{len(mesh.all())} agent(s)")


def team_tools() -> list[Tool]:
    """Messaging tools, which teammates get too."""
    return [SendMessageTool(), InboxTool(), TeamTool()]


def lead_tools() -> list[Tool]:
    """Everything the lead agent needs to assemble and run a team."""
    return [SpawnAgentTool(), *team_tools()]
