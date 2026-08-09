"""The bridge between one browser connection and one agent session.

Everything the terminal UI does through widgets, this does through JSON. The
agent, router, permission engine, session store and every tool are shared
with the TUI unchanged — only the presentation differs, which is the whole
reason the UI layer was kept thin.

The awkward part of a web frontend is that some agent operations *block on a
human*: a permission prompt, a clarifying question, a plan approval. Those
are handled with pending futures: the backend sends a message, parks a
future, and the frontend's reply resolves it. A disconnect cancels every
outstanding future rather than leaving the agent waiting forever.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..agent.heartbeat import Heartbeat, HeartbeatState, StopReason
from ..agent.loop import (
    Agent,
)
from ..agent.mesh import AgentMesh
from ..config.schema import Config
from ..constants import HEARTBEAT_DEFAULT_INTERVAL
from ..edits import EditReviewBoard
from ..mcp.manager import MCPManager
from ..permissions import PermissionEngine, Verdict
from ..providers import proxy
from ..providers.router import Router
from ..session.store import SessionHandle, SessionStore
from ..setup import readiness, role_table
from ..skills import SkillLibrary
from ..skills import default_skill_roots as _skill_roots
from ..toolset import build_registry
from .claude_runtime import ClaudeRuntime
from .codex_runtime import CodexRuntime, HOME_KIMI
from .drafts import DraftStore
from .protocol import (
    ConfigPayload,
    Outbound,
    StatusPayload,
    message,
)


@dataclass
class LiveTurn:
    """A turn that keeps running after the user opens another chat."""

    session_id: str
    handle: SessionHandle
    agent: Agent
    task: asyncio.Task | None = None
    #: Last activity line for this turn (restored when the user switches back).
    last_activity: str = ""
    #: Second interrupt (or shutdown) escalates to ``task.cancel()``.
    hard_cancel: bool = False
    #: True once ``Outbound.DONE`` was pushed for this turn (cancel-safe).
    done_sent: bool = False


@dataclass
class PendingHitl:
    """A parked Ask / Plan / Permission prompt waiting on the human."""

    session_id: str
    kind: Outbound
    payload: dict[str, Any] = field(default_factory=dict)

#: How a pending human decision is identified across the wire.
PendingKey = str
#: Pushes one message to the connected frontend.
Sender = Callable[[dict[str, Any]], Awaitable[None]]
#: Seconds a parked question waits before giving up on the browser.
HUMAN_REPLY_TIMEOUT = 600.0
#: Characters of a tool argument rendered into its one-line headline.
HEADLINE_ARG_CHARS = 90
#: Project directories remembered for the quick-switch list.
RECENT_WORKSPACES = 12
#: Argument keys worth showing on a tool's headline, in preference order.
HEADLINE_KEYS = ("command", "file_path", "pattern", "path", "name", "goal", "question")


def tool_headline(name: str, args: dict[str, Any]) -> str:
    """Render a tool call as one compact line, the same way the TUI does."""
    for key in HEADLINE_KEYS:
        value = args.get(key)
        if value:
            text = " ".join(str(value).split())
            if len(text) > HEADLINE_ARG_CHARS:
                text = text[: HEADLINE_ARG_CHARS - 1] + "…"
            return f"{name}({text})"
    return f"{name}()"


class GuiSession:
    """One agent, driven by one browser connection."""

    def __init__(self, config: Config, workspace: Path, send: Sender):
        self.config = config
        self.workspace = workspace
        self._send = send
        #: False while a browser is attached; True after detach (turns keep running).
        self._client_detached = False

        self.router = Router(config)
        # Provider retries were silent; tip the sticky activity dock so a
        # brief outage does not look like a hung/dead send.
        self.router.on_retry = self._progress
        self.permissions = PermissionEngine(config.permissions, workspace)
        self.skills = SkillLibrary(workspace, config.skill_paths).load()
        self.sessions = SessionStore()
        self.drafts = DraftStore()
        self.mcp = MCPManager(config.mcp_servers)
        self.mesh = AgentMesh()
        self.market, self.paper_book = self._build_market()
        #: Per-chat edit review queues — two live turns must not share apply/reject.
        self._edit_reviews: dict[str, EditReviewBoard] = {}
        #: Relative @ paths from the most recent user prompt.
        self.last_turn_refs: list[str] = []
        #: Turns still running after the user switched chats (keyed by id).
        self.live: dict[str, LiveTurn] = {}
        #: Per-chat TodoWrite lists for this process (keyed by session id).
        self.session_todos: dict[str, list] = {}
        #: Session id of the turn currently streaming (viewed or background).
        self.stream_session_id: str | None = None
        #: Quest verify retry to start after the current turn fully ends.
        self._quest_retry_after: dict[str, str] = {}
        #: Auto-continue chains started for open todos (keyed by session id).
        self._auto_continues: dict[str, int] = {}

        self.session = self._new_session_handle()
        self.agent = self._build_agent()
        self._wire_context()

        self.heartbeat = Heartbeat(
            self._run_heartbeat_iteration,
            lambda: self.router.ledger.total_cost,
            on_stop=self._on_heartbeat_stop,
            on_beat=self._on_heartbeat_beat,
        )
        self.show_archived = False
        #: Caps accepted by the dialog but not yet spent on a goal. While this
        #: is set the composer is in goal mode: the next prompt starts the
        #: loop instead of running one turn.
        self.armed_limits: Any = None
        self.armed_interval = HEARTBEAT_DEFAULT_INTERVAL
        # Seeded from disk, not just the directory we happen to have opened
        # in. Without this the remembered projects only appeared after the
        # user switched workspace once, which made the sidebar look empty on
        # every restart.
        self.recent_workspaces: list[str] = self._load_recent_workspaces()
        self.plan: Any = None
        self._pending: dict[PendingKey, asyncio.Future] = {}
        self._pending_hitl: dict[PendingKey, PendingHitl] = {}
        #: Legacy single-task slot; prefer :attr:`live` entries.
        self._turn_task: asyncio.Task | None = None
        # Panels must not inherit the exe install cwd (…/dist/Dr.Wang).
        from .workspace import preferred_project_workspace

        panel_workspace = preferred_project_workspace(workspace)
        #: Sibling Codex app-server runtime (independent of Agent loop).
        self.codex = CodexRuntime(
            workspace=panel_workspace,
            push=self._push_codex,
            park_approval=self._park_codex_approval,
            home_kind=HOME_KIMI,
            agent_accounts=lambda: list(self.config.accounts),
        )
        #: Sibling Claude Code runtime (stream-json host shell).
        self.claude = ClaudeRuntime(
            workspace=panel_workspace,
            push=self._push_claude,
            park_approval=self._park_claude_approval,
            agent_accounts=lambda: list(self.config.accounts),
        )

    async def _push_codex(self, kind: str, payload: dict[str, Any]) -> None:
        """Forward a Codex panel event onto the WebSocket bus."""
        try:
            outbound = Outbound(kind)
        except ValueError:
            outbound = Outbound.CODEX_NOTICE
            payload = {"level": "warn", "text": f"unknown codex event {kind}", **payload}
        await self.push(outbound, **payload)

    async def _push_claude(self, kind: str, payload: dict[str, Any]) -> None:
        try:
            outbound = Outbound(kind)
        except ValueError:
            outbound = Outbound.CLAUDE_NOTICE
            payload = {"level": "warn", "text": f"unknown claude event {kind}", **payload}
        await self.push(outbound, **payload)

    async def _park_codex_approval(self, info: dict[str, Any]) -> str | None:
        """Ask the browser to approve a Codex command/file change."""
        answer = await self._park(
            Outbound.CODEX_PERMISSION,
            session_id=str(info.get("panel_session_id") or "codex"),
            tool=str(info.get("tool") or "Codex"),
            reason=str(info.get("reason") or ""),
            detail=str(info.get("detail") or ""),
            kind=str(info.get("kind") or ""),
            args={"detail": info.get("detail"), "params": info.get("params") or {}},
            panel_session_id=str(info.get("panel_session_id") or ""),
        )
        if isinstance(answer, dict):
            return str(answer.get("decision") or answer.get("value") or "")
        return str(answer) if answer is not None else None

    async def _park_claude_approval(self, info: dict[str, Any]) -> str | None:
        answer = await self._park(
            Outbound.CLAUDE_PERMISSION,
            session_id=str(info.get("panel_session_id") or "claude"),
            tool=str(info.get("tool") or "Claude Code"),
            reason=str(info.get("reason") or ""),
            detail=str(info.get("detail") or ""),
            kind=str(info.get("kind") or ""),
            args={"detail": info.get("detail"), "params": info.get("params") or {}},
            panel_session_id=str(info.get("panel_session_id") or ""),
        )
        if isinstance(answer, dict):
            return str(answer.get("decision") or answer.get("value") or "")
        return str(answer) if answer is not None else None

    @property
    def busy(self) -> bool:
        """Whether the *viewed* conversation has a turn in flight."""
        return self.session.meta.id in self.live

    @busy.setter
    def busy(self, value: bool) -> None:
        # Kept for call sites that still assign; live registry is authoritative.
        if not value and self.session.meta.id in self.live:
            return

    def ui_language(self) -> str:
        """Language preference from UI prefs (``auto`` or a concrete code)."""
        from ..ui.prefs import UIPrefs
        from .locale import normalize_language

        return normalize_language(UIPrefs.load().language)

    def language_choices(self) -> list[dict[str, str]]:
        from .locale import language_choices

        return language_choices()

    def set_ui_language(self, code: str) -> str:
        """Persist and return the normalised language preference."""
        from ..ui.prefs import UIPrefs
        from .locale import normalize_language

        language = normalize_language(code)
        prefs = UIPrefs.load()
        prefs.language = language
        prefs.save()
        return language

    def msg(self, key: str, **params: object) -> str:
        """Localised server notice for the current UI language preference."""
        from .messages import tr

        return tr(self.ui_language(), key, **params)

    # -- construction -----------------------------------------------------

    def _new_session_handle(self):
        """Start a fresh session, clearing away any abandoned empty ones.

        A session is created before the user has typed anything, so without
        the prune the sidebar accumulates identical untitled rows that cannot
        be distinguished — and deleting one looks like it did nothing.
        """
        # Never delete the conversation that is mid-turn (or still open).
        keep = ""
        if getattr(self, "session", None) is not None:
            keep = self.session.meta.id
        self.sessions.prune_empty(keep=keep)
        binding = self.config.role("main")
        return self.sessions.create(
            self.workspace,
            model=binding.model if binding else "",
            account=(binding.account or "") if binding else "",
            permission_mode=self.permissions.mode,
        )

    def _build_agent(self, handle: SessionHandle | None = None) -> Agent:
        """Build an agent with its own permission engine.

        Concurrent turns must not share ``plan_mode`` / session allow-rules —
        otherwise switching chats mid-plan silently unblocks writes on the
        background turn.
        """
        target = handle or self.session
        mode = target.meta.permission_mode or self.config.permissions.mode
        permissions = PermissionEngine(
            replace(self.config.permissions, mode=mode),  # type: ignore[arg-type]
            Path(target.meta.workspace) if target.meta.workspace else self.workspace,
        )
        agent = Agent(
            self.config,
            self.router,
            build_registry(
                include_desktop=self.config.desktop.enabled,
                include_browser=self.config.browser.enabled,
                include_market=self.config.market.enabled,
                extra_tools=self.mcp.tools,
            ),
            permissions,
            Path(target.meta.workspace) if target.meta.workspace else self.workspace,
            skills=self.skills,
            session=target,
        )
        # Restore this chat's task list (RAM cache, then durable todos.json).
        restored = list(
            self.session_todos.get(target.meta.id)
            or getattr(target, "todos", None)
            or []
        )
        agent.ctx.todos = restored
        self.session_todos[target.meta.id] = list(restored)
        return agent

    def rebuild_tools(self) -> None:
        """Re-derive the tool registry after a capability was toggled.

        The live agent keeps its transcript; only the tool schema changes.
        The system prompt is invalidated too, because the tool list is part
        of the cached prefix and a stale one would advertise tools that are
        no longer there.
        """
        self.agent.tools = build_registry(
            include_desktop=self.config.desktop.enabled,
            include_browser=self.config.browser.enabled,
            include_market=self.config.market.enabled,
            extra_tools=self.mcp.tools,
        )
        self.agent.invalidate_system_prompt()

    def _build_market(self):
        """Create the market router when the capability is enabled."""
        settings = self.config.market
        if not settings.enabled:
            return None, None
        from ..market.paper import PaperBook
        from ..market.router import MarketRouter

        router = MarketRouter(
            settings.qlib_store or None, allow_live=settings.live_quotes
        )
        paper_path = Path(settings.paper_file)
        if not paper_path.is_absolute():
            paper_path = self.workspace / paper_path
        return router, PaperBook(paper_path, settings.paper_cash)

    def _wire_context(self) -> None:
        """Bind HITL / progress callbacks to this agent’s conversation id."""
        self.permissions = self.agent.permissions
        sid = self.session.meta.id
        agent = self.agent

        async def approve(tool: str, args: dict[str, Any], verdict: Verdict) -> bool:
            return await self._ask_permission(
                tool, args, verdict, session_id=sid, agent=agent
            )

        async def ask_user(questions: list[Any]) -> dict[str, str]:
            return await self._ask_questions(questions, session_id=sid)

        async def present_plan(plan: Any) -> tuple[bool, str]:
            return await self._present_plan(plan, session_id=sid, agent=agent)

        def progress(line: str) -> None:
            self._progress(line, session_id=sid)

        agent.ctx.approve = approve
        agent.ctx.progress = progress
        agent.ctx.ask_user = ask_user
        agent.ctx.present_plan = present_plan
        agent.ctx.mesh = self.mesh
        agent.ctx.make_session = self._make_child_session
        agent.ctx.market = self.market
        agent.ctx.paper_book = self.paper_book
        agent.ctx.edit_review = self.edit_board(sid)

    def edit_board(self, session_id: str | None = None) -> EditReviewBoard:
        """Return the Write/Edit review queue for one chat."""
        sid = session_id or self.session.meta.id
        board = self._edit_reviews.get(sid)
        if board is None:
            board = EditReviewBoard()
            self._edit_reviews[sid] = board
        return board

    @property
    def edit_review(self) -> EditReviewBoard:
        """Review board for the currently viewed chat."""
        return self.edit_board()

    def drop_edit_board(self, session_id: str) -> None:
        self._edit_reviews.pop(session_id, None)

    async def push_edit_review(self, *, session_id: str | None = None) -> None:
        sid = session_id or self.session.meta.id
        await self.push(
            Outbound.EDIT_REVIEW,
            pending=self.edit_board(sid).public(),
            session_id=sid,
        )

    def _make_child_session(self, title: str):
        handle = self.sessions.create(self.workspace)
        handle.rename(title)
        asyncio.create_task(self.push_sessions())
        return handle

    # -- outbound ---------------------------------------------------------

    def bind_send(self, send: Sender) -> None:
        """Attach (or re-attach) the browser WebSocket sender."""
        self._send = send
        self._client_detached = False

    async def detach_client(self) -> None:
        """Browser left — keep live turns; abandon parked HITL prompts."""
        self.cancel_pending()
        self._client_detached = True

        async def _noop(_payload: dict[str, Any]) -> None:
            return None

        self._send = _noop

    async def on_client_attached(self) -> None:
        """Push full UI state after connect / reconnect."""
        await self.push_transcript()
        await self.push_all()
        await self.push_edit_review()
        await self.push_pending_hitl()
        await self.codex.push_status(include_transcript=True)
        await self.claude.push_status(include_transcript=True)
        viewed = self.session.meta.id
        live = self.live.get(viewed)
        if live is not None and live.last_activity:
            await self.push(
                Outbound.ACTIVITY,
                text=live.last_activity,
                source="main",
                session_id=viewed,
            )

    async def push(self, outbound: Outbound, **payload: Any) -> None:
        # Parameter must not be named ``kind`` — canvas/tool payloads use that key.
        sid = str(payload.get("session_id") or "")
        if outbound is Outbound.ACTIVITY and sid:
            text = str(payload.get("text") or "").strip()
            if text:
                self.note_activity(sid, text)
        try:
            await self._send(message(outbound, **payload))
        except Exception:  # noqa: BLE001 — never kill a turn because the UI left
            pass

    def note_activity(self, session_id: str, text: str) -> None:
        """Remember the latest activity line for a (possibly background) turn."""
        live = self.live.get(session_id)
        if live is not None and text:
            live.last_activity = text

    def _progress(self, line: str, *, session_id: str = "") -> None:
        """Surface a tool/subagent progress line in the transcript and status.

        Notices stay in the scrollback; the activity event drives the live
        "谁在干什么" row under the latest user message so parallel research
        or a long verify is visible without hunting the log.
        """
        asyncio.create_task(self._emit_progress(line, session_id=session_id))

    async def _emit_progress(self, line: str, *, session_id: str = "") -> None:
        sid = session_id or self.stream_session_id or self.session.meta.id
        # Shell "$ …" echoes already appear on the tool headline; keep them on
        # the sticky activity dock only so the transcript stays readable.
        if not line.lstrip().startswith("$ "):
            await self.push(Outbound.NOTICE, level="info", text=line, session_id=sid)
        source = "main"
        lowered = line.lower()
        if lowered.startswith("researching"):
            source = "research"
        elif lowered.startswith("verifying"):
            source = "verify"
        elif lowered.startswith("delegating"):
            source = "delegate"
        elif lowered.startswith("subagent"):
            source = "subagent"
        elif lowered.startswith("team:"):
            source = "team"
        elif lowered.startswith("adversarial"):
            source = "review"
        await self.push(
            Outbound.ACTIVITY, text=line, source=source, session_id=sid
        )

    async def push_status(self) -> None:
        model = self.config.model(self.agent.selection.model_id)
        effort = self.agent.selection.effort or (model.default_effort if model else "")
        if model is not None and not model.effort_levels():
            effort = ""
        ready, _ = readiness(self.config)
        viewed_id = self.session.meta.id
        live = self.live.get(viewed_id)
        activity = ""
        if live is not None:
            activity = (live.last_activity or "").strip()
            if not activity:
                label = live.agent.selection.label() if live.agent else viewed_id
                activity = f"{label} …"
        todos = list(
            self.session_todos.get(viewed_id)
            or self.agent.ctx.todos
            or self.session.todos
            or []
        )
        open_todos = [
            item
            for item in todos
            if str(item.get("status") or "") != "completed"
        ]
        from ..quest import load_quest

        quest = load_quest(self.workspace, session_id=viewed_id)
        quest_open = bool(
            quest is not None and quest.status not in {"idle", "done"}
        )
        busy = viewed_id in self.live
        status = StatusPayload(
            model=self.agent.selection.model_id,
            account=self.agent.selection.account_id or "",
            effort=effort,
            mode=self.permissions.mode,
            context_used=self.agent.context_used(),
            context_window=self.agent.context_window(),
            cache_hit=self.agent.state.cache.hit_rate,
            run_cache_hit=self.agent.state.cache.hit_rate,
            session_cache_hit=self.session.meta.cache_hit_rate,
            cost=self.router.ledger.total_cost,
            busy=busy,
            plan_mode=self.permissions.plan_mode,
            explore_mode=self.permissions.explore_mode,
            session_id=viewed_id,
            session_title=self.session.meta.title,
            heartbeat=self.heartbeat.active,
            heartbeat_armed=self.armed_limits is not None,
            configured=ready,
            workspace=str(self.workspace),
            workspace_name=self.workspace.name or str(self.workspace),
            auto_compact=self.config.context.auto_compact,
            auto_classify=self.config.planning.auto_classify,
            auto_apply_edits=self.config.ui.auto_apply_edits,
            language=self.ui_language(),
            draft=self._draft_text(),
            compact_threshold=self.config.context.compact_threshold,
            context_options=(model.context_windows if model else []),
            turn_refs=list(self.last_turn_refs),
            activity=activity,
            resume_available=(not busy) and (bool(open_todos) or quest_open),
            open_todo_count=len(open_todos),
        )
        await self.push(Outbound.STATUS, status=status.to_dict())
        await self.push_onboarding()

    async def push_onboarding(self) -> None:
        """Send a short setup checklist derived from readiness()."""
        from ..setup import readiness

        ready, problems = readiness(self.config)
        steps = []
        blob = " ".join(problems).lower()
        if "no api accounts" in blob or "account" in blob:
            steps.append({"id": "account", "label": self.msg("onboard.need_account")})
        if "no models" in blob:
            steps.append({"id": "model", "label": self.msg("onboard.need_model")})
        if "default conversation model" in blob or "main" in blob:
            steps.append({"id": "role", "label": self.msg("onboard.need_role")})
        if not steps and not ready:
            steps = [{"id": "setup", "label": p} for p in problems[:4]]
        await self.push(
            Outbound.ONBOARDING,
            ready=ready,
            steps=steps,
            problems=problems,
            message=self.msg("onboard.ready") if ready else "",
        )

    def _draft_text(self) -> str:
        return self.drafts.get(self.session.meta.id)

    async def push_context(self) -> None:
        from ..agent.prompts import PROJECT_INSTRUCTION_FILES
        from ..rules import PROJECT_RULES_DIR, global_rules_dir

        breakdown = self.agent.context_breakdown()
        project_files = [
            name
            for name in PROJECT_INSTRUCTION_FILES
            if (self.workspace / name).is_file()
        ]
        await self.push(
            Outbound.CONTEXT,
            window=breakdown.window,
            used=breakdown.used,
            free=breakdown.free,
            fraction=breakdown.fraction,
            rows=[
                {"name": name, "tokens": tokens, "share": share}
                for name, tokens, share in breakdown.rows()
            ],
            detail={
                item.name: item.detail for item in breakdown.slices if item.detail
            },
            rules=getattr(self.agent, "_rules_sources", []) or [],
            memories=getattr(self.agent, "_memory_sources", []) or [],
            project_instructions=project_files,
            rules_dirs=[
                str(global_rules_dir()),
                str(self.workspace / PROJECT_RULES_DIR),
            ],
            turn_refs=list(self.last_turn_refs),
        )

    def remember_workspace(self, path: Path) -> None:
        """Register a project directory, in memory and on disk together.

        Both copies are written here because they drifted when they were
        updated separately: the handler persisted the change while this list
        kept the old contents, so a freshly opened project was missing from
        the sidebar until the next restart.
        """
        from .workspace import RecentWorkspaces, is_app_install_workspace

        if is_app_install_workspace(path):
            return
        entry = str(path)
        recents = [p for p in self.recent_workspaces if p != entry]
        self.recent_workspaces = [entry, *recents][:RECENT_WORKSPACES]

        stored = RecentWorkspaces.load()
        stored.remember(path)
        stored.save()

    def _load_recent_workspaces(self) -> list[str]:
        """The persisted project list, exactly as the user left it.

        The directory the app happens to start in is deliberately *not*
        prepended. Doing that resurrected a project the user had removed on
        the very next launch, because the app starts in that directory — the
        removal persisted correctly and was then undone here.

        A project is registered by starting a conversation in it, and any
        directory holding sessions is listed regardless of this list, so
        nothing is lost by keeping this list strictly user-chosen.
        """
        from .workspace import RecentWorkspaces

        return RecentWorkspaces.load().paths[:RECENT_WORKSPACES]

    def live_workspaces(self) -> list[str]:
        """Remembered projects that still exist on disk.

        A directory that has been deleted or renamed is not a shortcut, it is
        a click that fails. Filtering happens on the way out rather than on
        the way in, because a project on a drive that is merely unplugged
        should come back when it is plugged in again.
        """
        return [p for p in self.recent_workspaces if Path(p).is_dir()]

    def forget_workspace(self, path: Path) -> None:
        """Drop a project directory from the quick-switch list."""
        entry = str(path)
        self.recent_workspaces = [
            p for p in self.recent_workspaces if p != entry and Path(p) != path
        ]

    async def push_skills(self) -> None:
        """Send the installed skills and every directory they were found in."""
        roots = [str(root) for root, _ in _skill_roots(self.workspace)]
        await self.push(
            Outbound.SKILLS,
            skills=[
                {
                    "name": skill.name,
                    "description": " ".join(skill.description.split()),
                    "source": skill.source,
                    "path": str(skill.path),
                }
                for skill in self.skills.all()
            ],
            roots=roots,
            paths=list(self.config.skill_paths),
            errors=list(self.skills.errors),
        )

    def _session_row(self, meta) -> dict[str, Any]:
        waiting = any(
            info.session_id == meta.id for info in self._pending_hitl.values()
        )
        return {
            "id": meta.id,
            "title": meta.title or "(未命名)",
            "updated": meta.updated_label,
            "messages": meta.message_count,
            "cost": meta.total_cost,
            "archived": meta.archived,
            "active": meta.id == self.session.meta.id,
            "running": meta.id in self.live,
            "waiting": waiting,
        }

    async def push_sessions(self) -> None:
        """Send every project and the sessions inside it.

        Grouped by workspace rather than flat: one directory is one project,
        and a session only makes sense against the tree it was run in.
        """
        current = str(self.workspace)
        known = {str(path): count for path, count in self.sessions.workspaces()}
        # The open directory is *not* pinned into the list. It used to be, and
        # that made the last project undeletable: removing it dropped it from
        # the remembered list, and this line put it straight back. A project
        # is something the user chose to keep, not wherever the agent happens
        # to be pointed — the chip row above the composer already shows that.
        # Empty/mid-turn chats stay visible via ``keep=`` below once the
        # project is already listed (remembered or has other sessions).
        for extra in self.live_workspaces():
            known.setdefault(extra, 0)

        groups = []
        for path in sorted(known, key=lambda p: (p != current, p.lower())):
            entries = self.sessions.list(
                workspace=Path(path),
                include_archived=self.show_archived,
                include_empty=False,
                # Keep the open chat visible even before its first message is
                # on disk (or while the first turn is still streaming). Without
                # this the sidebar says "还没有会话" mid-turn, and 「新会话」
                # can strand the busy conversation.
                keep=self.session.meta.id if path == current else "",
            )
            groups.append(
                {
                    "path": path,
                    "name": Path(path).name or path,
                    "active": path == current,
                    "sessions": [self._session_row(meta) for meta in entries],
                }
            )

        archived_count = len(
            [
                meta
                for meta in self.sessions.list(limit=10**6, include_archived=True)
                if meta.archived
            ]
        )
        await self.push(
            Outbound.SESSIONS,
            workspaces=groups,
            show_archived=self.show_archived,
            archived_count=archived_count,
        )

    async def push_config(self) -> None:
        ready, problems = readiness(self.config)
        payload = ConfigPayload(
            accounts=[
                {
                    "id": account.id,
                    "base_url": account.base_url,
                    "key": account.masked_key(),
                    "env": account.api_key_env,
                    "enabled": account.enabled,
                    "proxy": account.proxy,
                    "proxy_label": proxy.describe(account),
                    "models": [m.id for m in self.config.models if account.id in m.accounts],
                }
                for account in self.config.accounts
            ],
            models=[
                {
                    "id": model.id,
                    "model": model.model,
                    "accounts": model.accounts,
                    "context_windows": model.context_windows,
                    "default_context": model.context_for(),
                    "effort_levels": model.effort_levels(),
                    "supports_vision": self._model_vision(model),
                    "vision_mode": getattr(model, "vision_mode", "auto") or "auto",
                    "vision_detected": bool(model.supports_vision)
                    or self._model_vision_heuristic(model),
                }
                for model in self.config.models
            ],
            roles=[
                {"role": role, "binding": binding, "explicit": explicit}
                for role, binding, explicit in role_table(self.config)
            ],
            capabilities=self._capabilities(),
            proxy_presets=list(proxy.COMMON_PROXY_URLS),
            languages=self.language_choices(),
            support=self._support_payload(),
            ready=ready,
            problems=problems,
        )
        await self.push(Outbound.CONFIG, config=payload.to_dict())

    def _model_vision(self, model: Any) -> bool:
        from ..session.attachments import model_supports_vision

        return model_supports_vision(model)

    def _model_vision_heuristic(self, model: Any) -> bool:
        from ..session.attachments import infer_vision_capability

        return infer_vision_capability(model)

    def _support_payload(self) -> dict[str, Any]:
        """Public donate links; never includes API credentials."""
        from ..support import public_support

        data = public_support()
        if not data.get("enabled"):
            data["alipay_available"] = False
            data["wechat_available"] = False
            return data
        donate_dir = Path(__file__).parent / "web" / "donate"
        alipay_name = Path(str(data.get("alipay_qr") or "")).name or "alipay.png"
        wechat_name = Path(str(data.get("wechat_qr") or "")).name or "wechat.png"
        data["alipay_available"] = (donate_dir / alipay_name).is_file()
        data["wechat_available"] = (donate_dir / wechat_name).is_file()
        return data

    def _capabilities(self) -> list[dict[str, Any]]:
        """The opt-in tool groups, with enough text to decide in the UI.

        These are described rather than merely named because "allow desktop
        control" is not a setting anybody can evaluate from three words: what
        matters is that the agent acts outside the workspace and decides what
        to click from pixels it did not produce.
        """
        lang = self.ui_language()
        from .messages import resolve_ui_locale

        loc = resolve_ui_locale(lang)
        if loc == "en":
            desktop = {
                "name": "Desktop control",
                "detail": (
                    "Screenshot, move the mouse, click and type. Affects the whole "
                    "screen, not just the workspace; the model decides clicks from pixels."
                ),
                "note": f"Every step requires approval (mode {self.config.desktop.require_mode})",
            }
            browser = {
                "name": "Browser control",
                "detail": (
                    "Launch Chromium, open pages, click, fill forms, read content. "
                    "Page text is treated as data, never as instructions."
                ),
                "note": (
                    "Allowed domains: " + ", ".join(self.config.browser.allow_domains)
                    if self.config.browser.allow_domains
                    else "No domain allow-list"
                ),
            }
        elif loc == "ja":
            desktop = {
                "name": "デスクトップ制御",
                "detail": "画面キャプチャ・マウス・クリック・キー入力。ワークスペース外にも作用します。",
                "note": f"各操作は確認が必要（モード {self.config.desktop.require_mode}）",
            }
            browser = {
                "name": "ブラウザ制御",
                "detail": "Chromium を起動し、閲覧・クリック・入力・読み取り。ページ本文はデータとして扱います。",
                "note": (
                    "許可ドメイン：" + "、".join(self.config.browser.allow_domains)
                    if self.config.browser.allow_domains
                    else "ドメイン制限なし"
                ),
            }
        else:
            desktop = {
                "name": "控制桌面",
                "detail": (
                    "允许截屏、移动鼠标、点击和按键。作用于整个屏幕，"
                    "不限于工作目录；模型依据截图内容决定点哪里。"
                ),
                "note": f"当前每步都需确认（权限模式 {self.config.desktop.require_mode}）",
            }
            browser = {
                "name": "控制浏览器",
                "detail": (
                    "启动内置 Chromium，打开网页、点击、填表并读取页面内容。"
                    "网页正文一律当作数据，不当作指令。"
                ),
                "note": (
                    "限定域名：" + "、".join(self.config.browser.allow_domains)
                    if self.config.browser.allow_domains
                    else "未限制域名"
                ),
            }
        return [
            {
                "id": "desktop",
                "enabled": self.config.desktop.enabled,
                **desktop,
            },
            {
                "id": "browser",
                "enabled": self.config.browser.enabled,
                **browser,
            },
        ]

    async def push_transcript(self) -> None:
        """Replay the open session so a reconnect shows the conversation."""
        from ..providers.base import message_text
        from ..session.attachments import refs_from_meta

        entries: list[dict[str, Any]] = []
        session_id = self.session.meta.id
        for msg in self.agent.messages:
            if msg.meta.get("compacted"):
                continue
            if msg.role == "user":
                text = msg.meta.get("user_text") or message_text(msg.content)
                images = [
                    {
                        "name": str(ref.get("name") or ""),
                        "mime": str(ref.get("mime") or "image/png"),
                        "url": f"/attachment/{session_id}/{Path(str(ref.get('file') or '')).name}",
                    }
                    for ref in refs_from_meta(msg.meta)
                    if ref.get("file")
                ]
                entries.append({"role": "user", "text": text, "images": images})
            elif msg.role == "assistant" and msg.content:
                entries.append(
                    {"role": "assistant", "text": message_text(msg.content)}
                )
        markers = [
            {
                "summary": record.summary,
                "before": record.tokens_before,
                "after": record.tokens_after,
                "replaced": record.replaced_through,
            }
            for record in self.session.compactions
        ]
        await self.push(Outbound.READY, transcript=entries, compactions=markers)

    async def push_all(self) -> None:
        await self.push_status()
        await self.push_sessions()
        await self.push_config()
        await self.push(
            Outbound.WORKSPACE,
            path=str(self.workspace),
            name=self.workspace.name or str(self.workspace),
            recents=self.live_workspaces(),
        )
        await self.push_edit_review()
        # Todos belong to the open chat's agent — always refresh (including
        # empty) so a previous session's list cannot stick to the composer.
        await self.push(
            Outbound.TODOS,
            todos=list(self.agent.ctx.todos or []),
            session_id=self.session.meta.id,
        )
        from ..quest import load_quest

        sid = self.session.meta.id
        quest = load_quest(self.workspace, session_id=sid)
        await self.push(
            Outbound.QUEST,
            quest=quest.public() if quest else None,
            session_id=sid,
        )

    # -- human-in-the-loop ------------------------------------------------

    async def _park(
        self, kind: Outbound, *, session_id: str = "", **payload: Any
    ) -> Any:
        """Send a question and wait for the frontend's answer.

        Returns:
          The answer, or ``None`` if the browser never replied.
        """
        key = uuid.uuid4().hex[:8]
        sid = session_id or self.stream_session_id or self.session.meta.id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[key] = future
        body = {**payload, "session_id": sid}
        self._pending_hitl[key] = PendingHitl(
            session_id=sid, kind=kind, payload=dict(body)
        )
        await self.push(kind, id=key, **body)
        await self.push_sessions()
        timeout = HUMAN_REPLY_TIMEOUT
        configured = float(getattr(self.permissions.cfg, "prompt_timeout", 0) or 0)
        if configured > 0:
            timeout = configured
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            await self.push(
                Outbound.NOTICE,
                level="error",
                text=self.msg("hitl.timeout"),
                session_id=sid,
            )
            return None
        except asyncio.CancelledError:
            await self.push(
                Outbound.NOTICE,
                level="warn",
                text=self.msg("hitl.disconnected"),
                session_id=sid,
            )
            return None
        finally:
            self._pending.pop(key, None)
            self._pending_hitl.pop(key, None)
            await self.push_sessions()

    def resolve(self, key: str, value: Any) -> None:
        """Deliver a frontend reply to whichever call is waiting for it."""
        future = self._pending.pop(key, None)
        self._pending_hitl.pop(key, None)
        if future is not None and not future.done():
            future.set_result(value)

    def cancel_pending(self) -> None:
        """Abandon every parked question, e.g. when the browser disconnects."""
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()
        self._pending_hitl.clear()

    async def push_pending_hitl(self) -> None:
        """Re-show Ask/Plan/Permission cards that belong to the open chat."""
        sid = self.session.meta.id
        for key, info in list(self._pending_hitl.items()):
            if info.session_id != sid:
                continue
            await self.push(info.kind, id=key, **info.payload)

    async def _ask_permission(
        self,
        tool: str,
        args: dict[str, Any],
        verdict: Verdict,
        *,
        session_id: str = "",
        agent: Agent | None = None,
    ) -> bool:
        owner = agent or self.agent
        answer = await self._park(
            Outbound.PERMISSION,
            session_id=session_id,
            tool=tool,
            args=args,
            headline=tool_headline(tool, args),
            reason=verdict.reason,
            suggested_rule=verdict.suggested_rule,
        )
        if answer == "always" and verdict.suggested_rule:
            owner.permissions.allow_persistently(verdict.suggested_rule)
            try:
                from ..config import save_config

                save_config(self.config)
            except OSError:
                pass
        return answer in ("once", "always")

    async def _ask_questions(
        self, questions: list[Any], *, session_id: str = ""
    ) -> dict[str, str]:
        answer = await self._park(
            Outbound.ASK,
            session_id=session_id,
            questions=[
                {
                    "question": q.question,
                    "header": q.header,
                    "options": q.options,
                    "multi_select": q.multi_select,
                }
                for q in questions
            ],
        )
        return answer if isinstance(answer, dict) else {}

    async def _present_plan(
        self,
        plan: Any,
        *,
        session_id: str = "",
        agent: Agent | None = None,
    ) -> tuple[bool, str]:
        owner = agent or self.agent
        self.plan = plan
        answer = await self._park(
            Outbound.PLAN,
            session_id=session_id,
            plan={
                "goal": plan.goal,
                "revision": plan.revision,
                "steps": [
                    {
                        "title": step.title,
                        "detail": step.detail,
                        "files": step.files,
                        "model": step.model,
                    }
                    for step in plan.steps
                ],
                "risks": plan.risks,
                "open_questions": plan.open_questions,
                "out_of_scope": plan.out_of_scope,
            },
        )
        if not isinstance(answer, dict):
            return False, "The window closed without a decision."
        approved = bool(answer.get("approved"))
        if approved:
            plan.approved = True
            owner.permissions.set_plan_mode(False)
            owner.invalidate_system_prompt()
        return approved, str(answer.get("feedback", ""))

    # -- workspace --------------------------------------------------------

    def point_workspace(self, path: Path) -> Path:
        """Retarget directory-scoped state without inventing a conversation.

        Used when opening an existing chat in another project: creating a
        throwaway empty session first used to race a live turn and paint its
        stream onto the wrong transcript.

        Raises:
          NotADirectoryError: If the path is not a usable directory.
        """
        resolved = path.expanduser().resolve()
        if not resolved.is_dir():
            raise NotADirectoryError(f"{resolved} is not a directory")

        self.workspace = resolved
        self.remember_workspace(resolved)
        self.permissions = PermissionEngine(self.config.permissions, resolved)
        self.skills = SkillLibrary(resolved, self.config.skill_paths).load()
        self.market, self.paper_book = self._build_market()
        return resolved

    def set_workspace(self, path: Path) -> None:
        """Point the agent at a different project directory.

        Everything scoped to a directory is rebuilt: the permission engine's
        boundary, the skill search paths, the session list and the agent
        itself. Sharing any of those across projects would let one project's
        rules apply to another's files.

        Raises:
          NotADirectoryError: If the path is not a usable directory.
        """
        self.point_workspace(path)
        self.session = self._new_session_handle()
        self.agent = self._build_agent()
        self._wire_context()

    async def push_workspace(self, recents: list[str]) -> None:
        await self.push(
            Outbound.WORKSPACE,
            path=str(self.workspace),
            name=self.workspace.name or str(self.workspace),
            recents=recents,
        )

    # -- heartbeat --------------------------------------------------------

    async def _run_heartbeat_iteration(self, prompt: str) -> str:
        """Run one automatic continuation.

        Provider failures propagate on purpose: the heartbeat's reconnect
        path needs to see a dropped connection rather than mistake it for a
        finished turn.
        """
        from ..quest import quest_prompt_hint
        from .commands import run_turn

        if self.busy:
            return ""  # a human turn is in flight; skip this beat
        hint = quest_prompt_hint(
            self.workspace, session_id=self.session.meta.id
        )
        wired = f"{hint}{prompt}".strip() if hint else prompt
        return await run_turn(self, wired, automatic=True)

    def _on_heartbeat_beat(self, state: HeartbeatState) -> None:
        asyncio.create_task(
            self.push(
                Outbound.HEARTBEAT,
                active=True,
                goal=state.goal,
                iterations=state.iterations,
                remaining=state.remaining(self.router.ledger.total_cost),
            )
        )

    def _on_heartbeat_stop(self, state: HeartbeatState, reason: StopReason) -> None:
        asyncio.create_task(
            self.push(
                Outbound.HEARTBEAT,
                active=False,
                reason=reason.value,
                reason_zh=reason.label_zh,
                iterations=state.iterations,
                spent=state.spent(self.router.ledger.total_cost),
            )
        )

    # -- shutdown ---------------------------------------------------------

    async def close(self) -> None:
        """Full process shutdown — cancels every live turn and closes clients."""
        self.heartbeat.stop(StopReason.USER_STOPPED)
        self.cancel_pending()
        for live in list(self.live.values()):
            live.hard_cancel = True
            live.agent.interrupt()
            if live.task is not None:
                live.task.cancel()
        if self._turn_task is not None:
            self._turn_task.cancel()
        self.live.clear()
        await self.codex.stop()
        await self.claude.stop()
        await self.mcp.close()
        await self.router.aclose()
