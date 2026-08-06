"""Tool interface, execution context and registry."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..constants import MAX_SUBAGENT_DEPTH

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..config.schema import Config
    from ..permissions import PermissionEngine, Verdict
    from ..providers.router import Router
    from ..skills import SkillLibrary


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    # Short line shown in the transcript instead of the full payload.
    summary: str = ""
    # Anything the UI wants to render specially (diffs, todo lists, ...).
    display: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def error(cls, message: str) -> ToolResult:
        return cls(content=message, is_error=True, summary=message.split("\n", maxsplit=1)[0][:120])


# Callback the UI installs so tools can ask the user for approval.
# Receives (tool_name, args, verdict) and returns True to proceed.
ApprovalCallback = Callable[[str, dict[str, Any], "Verdict"], Awaitable[bool]]
# Callback for streaming progress lines out of long-running tools.
ProgressCallback = Callable[[str], None]
# Asks the user multiple-choice questions. Returns {header: answer}, or {} if
# the user dismissed them.
QuestionCallback = Callable[[list[Any]], Awaitable[dict[str, str]]]
# Presents a plan. Returns (approved, feedback).
PlanCallback = Callable[[Any], Awaitable[tuple[bool, str]]]


@dataclass
class ToolContext:
    workspace: Path
    config: Config
    permissions: PermissionEngine
    router: Router
    skills: SkillLibrary | None = None
    approve: ApprovalCallback | None = None
    progress: ProgressCallback | None = None
    ask_user: QuestionCallback | None = None
    present_plan: PlanCallback | None = None
    # The plan under discussion, once one exists.
    plan: Any = None
    plan_revision: int = 0
    # The mesh of cooperating agents, when one is active, and this agent's
    # place in it.
    mesh: Any = None
    identity: Any = None
    # The live browser session, created on first use.
    browser: Any = None
    # Creates a persisted child session; supplied by the UI so spawned agents
    # show up in the session list rather than vanishing into a subprocess.
    make_session: Any = None
    # Price data and the paper account, when market access is enabled.
    market: Any = None
    paper_book: Any = None
    # Files the agent has read this session; Edit refuses to touch others.
    read_files: dict[str, float] = field(default_factory=dict)
    # Shared todo state.
    todos: list[dict[str, Any]] = field(default_factory=list)
    # Post-edit Apply/Reject board (GUI); None in headless/subagent paths.
    edit_review: Any = None
    # Set by the agent loop around each tool invoke so edits can bind call_id.
    current_call_id: str = ""
    # Depth guard so subagents cannot recurse forever.
    depth: int = 0
    max_depth: int = MAX_SUBAGENT_DEPTH
    # Set when the user interrupts; long tools poll it.
    cancel: asyncio.Event = field(default_factory=asyncio.Event)

    def resolve(self, raw: str) -> Path:
        """Resolve a possibly relative path against the workspace."""
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = self.workspace / p
        return p

    def rel(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.workspace))
        except ValueError:
            return str(path)

    def note(self, line: str) -> None:
        if self.progress:
            self.progress(line)


class Tool:
    """Base class. Subclasses declare a name, description and JSON schema."""

    name: str = ""
    description: str = ""
    # Set for tools whose results are large and compactable.
    bulky: bool = False
    # Tools not exposed to subagents (e.g. spawning more subagents).
    subagent_safe: bool = True

    def schema(self) -> dict[str, Any]:
        raise NotImplementedError

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raise NotImplementedError

    # -- helpers ----------------------------------------------------------

    def spec(self) -> dict[str, Any]:
        """The OpenAI function-tool definition."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description.strip(),
                "parameters": self.schema(),
            },
        }

    async def guarded_run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Permission check, then run."""
        from ..permissions import Decision

        verdict = ctx.permissions.check(self.name, args)
        if verdict.decision is Decision.DENY:
            return ToolResult.error(f"Permission denied: {verdict.reason}")
        if verdict.decision is Decision.ASK:
            if ctx.approve is None:
                return ToolResult.error(
                    f"Permission required ({verdict.reason}) but no approval channel is available. "
                    f"Switch to auto/yolo mode or add an allow rule."
                )
            ok = await ctx.approve(self.name, args, verdict)
            if not ok:
                return ToolResult.error(
                    f"User declined {self.name}. Do not retry the same call; "
                    f"ask what to do differently."
                )
        try:
            return await self.run(args, ctx)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # tools must never crash the agent loop
            return ToolResult.error(f"{self.name} failed: {type(e).__name__}: {e}")


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None):
        self._tools: dict[str, Tool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError("tool has no name")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def all(self) -> list[Tool]:
        return [self._tools[n] for n in sorted(self._tools)]

    def specs(self, *, subagent: bool = False, exclude: set[str] | None = None) -> list[dict[str, Any]]:
        exclude = exclude or set()
        out = []
        for tool in self.all():
            if tool.name in exclude:
                continue
            if subagent and not tool.subagent_safe:
                continue
            out.append(tool.spec())
        return out

    def subset(self, names: list[str]) -> ToolRegistry:
        return ToolRegistry([self._tools[n] for n in names if n in self._tools])
