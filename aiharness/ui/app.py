"""The Textual application."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Header, Input, Static

from ..agent.heartbeat import Heartbeat, HeartbeatState, StopReason
from ..agent.loop import (
    Agent,
    Compacted,
    Done,
    Notice,
    Text,
    Thinking,
    ToolEnd,
    ToolStart,
    TurnEnd,
)
from ..agent.mesh import AgentMesh
from ..agent.planning import build_classifier_context, classify_request
from ..config.schema import Config
from ..constants import APP_NAME, UI_COMPLETION_LIMIT, UI_SCROLLBACK_LIMIT
from ..mcp.manager import MCPManager
from ..permissions import PermissionEngine, Verdict
from ..providers.router import Router, Selection
from ..scheduler.jobs import JobStore, RunRecord, ScheduledJob
from ..scheduler.runner import Scheduler
from ..session.store import SessionStore
from ..skills import SkillLibrary
from ..toolset import build_registry
from . import commands
from .mascot import Mascot, PetState
from .modals import ConfirmModal, PermissionModal, PlanModal, QuestionModal
from .prefs import UIPrefs
from .theme import DEFAULT_THEME, THEMES, get_theme
from .widgets import (
    AssistantMessage,
    CompactionDivider,
    ContextPanel,
    NoticeLine,
    PetPanel,
    PlanPanel,
    ReasoningBlock,
    StatusBar,
    TodoPanel,
    ToolCallEntry,
    UserMessage,
)

WELCOME = f"""\
**{APP_NAME}** — type a request, or `/help` for commands.

`/model` switch model or pin an API account · `/mode` permission mode ·
`/theme` colours · `/pet` mascot · `/job` scheduled tasks · `/cost` spend
"""

#: Seconds of inactivity before the mascot dozes off.
PET_SLEEP_AFTER = 240.0


class HarnessApp(App[None]):
    """Interactive terminal UI for the agent."""

    CSS_PATH = "styles.tcss"
    TITLE = APP_NAME

    BINDINGS = [
        ("ctrl+d", "quit", "Quit"),
        ("ctrl+c", "interrupt", "Interrupt"),
        ("ctrl+b", "toggle_sidebar", "Sidebar"),
        ("ctrl+r", "toggle_reasoning", "Reasoning"),
        ("ctrl+l", "clear_screen", "Clear screen"),
        ("ctrl+t", "cycle_theme", "Theme"),
        ("ctrl+g", "toggle_context", "Context"),
    ]

    def __init__(
        self,
        config: Config,
        workspace: Path,
        *,
        session_id: str | None = None,
        chinese: bool = False,
    ):
        super().__init__()
        self.config = config
        self.workspace = workspace
        self.prefs = UIPrefs.load()
        self.chinese = chinese or self.prefs.language == "zh"

        self.router = Router(config)
        self.permissions = PermissionEngine(config.permissions, workspace)
        self.skills = SkillLibrary(workspace, config.skill_paths).load()
        self.sessions = SessionStore()
        self.jobs = JobStore()
        self.scheduler: Scheduler | None = None
        self.mcp = MCPManager(config.mcp_servers)
        self.mesh = AgentMesh()
        self.market, self.paper_book = self._build_market()
        self.heartbeat = Heartbeat(
            self._run_heartbeat_iteration,
            lambda: self.router.ledger.total_cost,
            on_stop=self._on_heartbeat_stop,
            on_beat=self._on_heartbeat_beat,
        )

        self.session = self._open_session(session_id)
        self.agent = self._build_agent()
        self.mascot = Mascot(chinese=self.chinese, style=self.prefs.pet_style)

        self._assistant: AssistantMessage | None = None
        self._reasoning: ReasoningBlock | None = None
        self._tool_widgets: dict[str, ToolCallEntry] = {}
        self._show_reasoning = self.prefs.show_reasoning and config.ui.show_reasoning
        self._busy = False
        self._idle_timer = None
        self.plan_mode = False
        self.plan: Any = None
        self._classified_once = False

    # -- construction -----------------------------------------------------

    def _open_session(self, session_id: str | None):
        if session_id:
            existing = self.sessions.open(session_id)
            if existing is not None:
                return existing
        binding = self.config.role("main")
        return self.sessions.create(
            self.workspace,
            model=binding.model if binding else "",
            account=binding.account or "" if binding else "",
        )

    def _build_agent(self) -> Agent:
        return Agent(
            self.config,
            self.router,
            self._build_tool_registry(),
            self.permissions,
            self.workspace,
            skills=self.skills,
            session=self.session,
        )

    def _build_tool_registry(self):
        """Build the tool registry for this session.

        Named defensively: ``_registry`` is taken by Textual's ``App``.
        """
        return build_registry(
            include_desktop=self.config.desktop.enabled,
            include_browser=self.config.browser.enabled,
            include_market=self.config.market.enabled,
            extra_tools=self.mcp.tools,
        )

    def _build_market(self):
        """Create the market router and paper account, if enabled.

        Returns:
          A ``(router, paper_book)`` pair; both ``None`` when market access
          is off, which is the default.
        """
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
        """Point the agent's tool context at this app's interactive callbacks."""
        self.agent.ctx.approve = self._approve
        self.agent.ctx.progress = self._on_progress
        self.agent.ctx.ask_user = self._ask_user
        self.agent.ctx.present_plan = self._present_plan
        self.agent.ctx.mesh = self.mesh
        self.agent.ctx.make_session = self._make_child_session
        self.agent.ctx.market = self.market
        self.agent.ctx.paper_book = self.paper_book

    # -- heartbeat --------------------------------------------------------

    async def _run_heartbeat_iteration(self, prompt: str) -> str:
        """Run one automatic iteration and return the agent's closing text.

        Raises whatever the provider raises, so the heartbeat's reconnect
        path can see a dropped connection and retry rather than treating it
        as a finished turn.
        """
        if self._busy:
            return ""  # a human turn is in flight; skip this beat
        final = ""
        self._busy = True
        self._set_pet(PetState.WORKING)
        try:
            async for event in self.agent.run(prompt):
                self._handle_event(event)
                if isinstance(event, Done):
                    final = event.text
                elif isinstance(event, Notice) and event.level == "error":
                    raise RuntimeError(event.text)
        finally:
            self._busy = False
            self._refresh_status()
            self._transcript().scroll_end(animate=False)
        return final

    def _on_heartbeat_beat(self, state: HeartbeatState) -> None:
        self._notice(
            f"heartbeat {state.iterations}/{state.limits.max_iterations} — "
            f"{state.remaining(self.router.ledger.total_cost)}"
        )

    def _on_heartbeat_stop(self, state: HeartbeatState, reason: StopReason) -> None:
        label = reason.label_zh if self.chinese else reason.value
        level = "info" if reason is StopReason.GOAL_REACHED else "warn"
        self._notice(
            f"heartbeat stopped: {label} "
            f"(after {state.iterations} iteration(s), "
            f"${state.spent(self.router.ledger.total_cost):.4f})",
            level,
        )
        self._set_pet(
            PetState.HAPPY if reason is StopReason.GOAL_REACHED else PetState.WORRIED
        )
        self._refresh_status()

    def _make_child_session(self, title: str):
        """Create a persisted session for a spawned teammate.

        Child sessions are real sessions on disk, so the user can open one
        with ``/resume`` and read exactly what that agent did rather than
        taking the lead agent's word for it.
        """
        handle = self.sessions.create(self.workspace)
        handle.rename(title)
        self._refresh_sidebar()
        return handle

    # -- layout -----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield PetPanel(self.mascot)
                yield Static("Sessions", classes="section-title")
                yield Static("", id="sidebar-sessions")
                yield Static("Scheduled", classes="section-title")
                yield Static("", id="sidebar-jobs")
            with Vertical():
                yield VerticalScroll(id="transcript")
                yield ContextPanel()
                yield PlanPanel()
                yield TodoPanel()
                yield Static("", id="completions")
                with Vertical(id="input-row"):
                    yield Input(placeholder="Ask, or /help", id="prompt")
        yield StatusBar()
        yield Footer()

    async def on_mount(self) -> None:
        self._install_themes()
        self._wire_context()
        if self.prefs.sidebar_visible:
            self.query_one("#sidebar").add_class("visible")
        await self._say(WELCOME)
        self._restore_transcript()
        self.ensure_scheduler()
        self._refresh_status()
        self._refresh_sidebar()
        self._refresh_pet()
        self._idle_timer = self.set_interval(PET_SLEEP_AFTER, self._doze)
        self.query_one("#prompt", Input).focus()
        if self.config.mcp_servers:
            self.connect_mcp()

    def _install_themes(self) -> None:
        """Register the bundled themes and apply the saved one."""
        for spec in THEMES.values():
            try:
                self.register_theme(spec.to_textual())
            except Exception:  # noqa: BLE001 - a bad theme must not block startup
                continue
        wanted = self.prefs.theme if get_theme(self.prefs.theme) else DEFAULT_THEME
        try:
            self.theme = wanted
        except Exception:  # noqa: BLE001
            self.theme = DEFAULT_THEME

    def _restore_transcript(self) -> None:
        """Replay a resumed session's messages into the transcript.

        Past compactions are replayed as dividers too, so reopening a long
        session shows where its memory was condensed rather than presenting a
        summary as if the user had written it.
        """
        markers = {
            record.replaced_through: record
            for record in (self.session.compactions if self.prefs.show_compaction_markers else [])
        }
        for index, message in enumerate(self.agent.messages):
            record = markers.get(index)
            if record is not None:
                self._mount_entry(self._divider_for(record))
            if message.meta.get("compacted"):
                continue  # the divider already stands for these
            if message.role == "user":
                text = message.meta.get("user_text") or message.content
                self._mount_entry(UserMessage(text))
            elif message.role == "assistant" and message.content:
                widget = AssistantMessage()
                self._mount_entry(widget)
                widget.append(message.content)
                widget.finish()
        # A compaction covering the whole transcript sorts after every message.
        for boundary, record in markers.items():
            if boundary >= len(self.agent.messages):
                self._mount_entry(self._divider_for(record))

    def _divider_for(self, record: Any) -> CompactionDivider:
        return CompactionDivider(
            record.summary,
            tokens_before=record.tokens_before,
            tokens_after=record.tokens_after,
            replaced=record.replaced_through,
            model=record.model,
            chinese=self.chinese,
        )

    # -- transcript plumbing ----------------------------------------------

    def _transcript(self) -> VerticalScroll:
        return self.query_one("#transcript", VerticalScroll)

    def _mount_entry(self, widget: Any) -> None:
        transcript = self._transcript()
        transcript.mount(widget)
        children = transcript.children
        if len(children) > UI_SCROLLBACK_LIMIT:
            for stale in children[: len(children) - UI_SCROLLBACK_LIMIT]:
                stale.remove()
        transcript.scroll_end(animate=False)

    async def _say(self, markdown: str, level: str = "info") -> None:
        widget = AssistantMessage()
        self._mount_entry(widget)
        widget.append(markdown)
        widget.finish()

    def _notice(self, text: str, level: str = "info") -> None:
        self._mount_entry(NoticeLine(text, level))

    def _on_progress(self, line: str) -> None:
        """Surface a progress line from a long-running tool or subagent."""
        self._notice(line)

    # -- input ------------------------------------------------------------

    async def on_input_changed(self, event: Input.Changed) -> None:
        panel = self.query_one("#completions", Static)
        matches = commands.completions(event.value, UI_COMPLETION_LIMIT)
        if not matches:
            panel.remove_class("visible")
            return
        panel.update("\n".join(f"{name}  —  {summary}" for name, summary in matches))
        panel.add_class("visible")

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        self.query_one("#completions", Static).remove_class("visible")
        if not text:
            return
        if text.startswith("/"):
            await self._handle_command(text)
            return
        if self._busy:
            self._notice("still working — press ctrl+c to interrupt", "warn")
            return
        self._mount_entry(UserMessage(text))
        self._run_turn(text)

    async def _handle_command(self, line: str) -> None:
        try:
            output = await commands.dispatch(self, line)
        except Exception as error:  # noqa: BLE001 - a bad command must not crash the UI
            output = f"Command failed: {type(error).__name__}: {error}"
        if output:
            await self._say(output)
        self._refresh_status()
        self._refresh_sidebar()

    # -- the turn ---------------------------------------------------------

    @work(exclusive=True)
    async def _run_turn(self, text: str) -> None:
        """Run one user turn, streaming events into the transcript."""
        self._busy = True
        self._assistant = None
        self._reasoning = None
        self._set_pet(PetState.THINKING)
        self._refresh_status()
        failed = False
        try:
            text = await self._route_by_complexity(text)
            async for event in self.agent.run(text):
                self._handle_event(event)
        except asyncio.CancelledError:
            self._notice("interrupted", "warn")
            failed = True
        except Exception as error:  # noqa: BLE001
            self._notice(f"{type(error).__name__}: {error}", "error")
            failed = True
        finally:
            if self._reasoning:
                self._reasoning.finish()
            if self._assistant:
                self._assistant.finish()
            self._busy = False
            self._set_pet(PetState.WORRIED if failed else PetState.HAPPY)
            self._refresh_status()
            self.query_one(TodoPanel).render_todos(self.agent.ctx.todos)
            self._transcript().scroll_end(animate=False)

    async def _route_by_complexity(self, text: str) -> str:
        """Size up the request and route it before the main model sees it.

        A trivial request should not acquire a plan, and a project should not
        start with an edit. Classification runs on the cheap model, so the
        overhead is a fraction of a cent and a second or so.

        Args:
          text: The user's request.

        Returns:
          The prompt to actually run, possibly with clarifying answers and a
          plan-mode instruction appended.
        """
        planning = self.config.planning
        if not (planning.enabled and planning.auto_classify) or self.plan_mode:
            return text

        binding = self.config.role(planning.classifier_role)
        if binding is None:
            return text

        context = build_classifier_context(self.agent.messages)
        verdict = await classify_request(
            text,
            self.router,
            Selection.from_binding(binding),
            context=context,
        )
        self._notice(
            f"{verdict.complexity.label_zh if self.chinese else verdict.complexity.value}"
            f" (score {verdict.score})"
            + (f" — {verdict.reason}" if verdict.reason else "")
        )

        # Match GUI: do not park after a routine score notice; yolo never asks.
        ask_ok = (
            verdict.needs_clarification
            and planning.ask_when_unclear
            and verdict.needs_plan
            and self.permissions.mode != "yolo"
        )
        if ask_ok:
            answers = await self._ask_user(verdict.questions)
            if answers:
                rendered = "\n".join(f"- {k}: {v}" for k, v in answers.items())
                text += f"\n\n<clarifications>\n{rendered}\n</clarifications>"

        if verdict.needs_plan and planning.require_plan_approval:
            self.set_plan_mode(True)
            self._notice(
                "plan mode: 先调研出方案，批准后才动文件（/plan off 可退出）"
                if self.chinese
                else "plan mode: investigate and propose before writing (/plan off to leave)",
                "warn",
            )
            text += (
                "\n\n[Plan mode is active. Writes are blocked. Investigate the "
                "codebase, then call PresentPlan with a concrete plan. AskUser "
                "and plan text must match the user's language. Do not "
                "attempt to edit anything until the user approves it.]"
            )
        return text

    def _handle_event(self, event: Any) -> None:
        if isinstance(event, Thinking):
            self._handle_thinking(event)
        elif isinstance(event, Text):
            self._handle_text(event)
        elif isinstance(event, ToolStart):
            self._handle_tool_start(event)
        elif isinstance(event, ToolEnd):
            self._handle_tool_end(event)
        elif isinstance(event, Compacted):
            self._handle_compacted(event)
        elif isinstance(event, Notice):
            self._notice(event.text, event.level)
            if event.level == "error":
                self._set_pet(PetState.WORRIED)
        elif isinstance(event, TurnEnd):
            self._refresh_status()
        elif isinstance(event, Done):
            if self._assistant:
                self._assistant.finish()
                self._assistant = None

    def _handle_compacted(self, event: Compacted) -> None:
        """Show where the context was condensed, and by how much."""
        self._set_pet(PetState.COMPACTING)
        if self._assistant is not None:
            self._assistant.finish()
            self._assistant = None
        if not self.prefs.show_compaction_markers:
            self._notice(
                f"compacted {event.tokens_before:,} → {event.tokens_after:,} tokens", "info"
            )
        else:
            self._mount_entry(
                CompactionDivider(
                    event.summary,
                    tokens_before=event.tokens_before,
                    tokens_after=event.tokens_after,
                    replaced=event.replaced,
                    model=event.model,
                    chinese=self.chinese,
                )
            )
        self._refresh_status()

    def _handle_thinking(self, event: Thinking) -> None:
        if not self._show_reasoning:
            return
        if self._reasoning is None:
            self._reasoning = ReasoningBlock()
            self._mount_entry(self._reasoning)
        self._reasoning.append(event.text)

    def _handle_text(self, event: Text) -> None:
        if self._reasoning is not None:
            self._reasoning.finish()
            self._reasoning = None
        if self._assistant is None:
            self._assistant = AssistantMessage()
            self._mount_entry(self._assistant)
        self._assistant.append(event.text)
        self._transcript().scroll_end(animate=False)

    def _handle_tool_start(self, event: ToolStart) -> None:
        if self._assistant is not None:
            self._assistant.finish()
            self._assistant = None
        if self._reasoning is not None:
            self._reasoning.finish()
            self._reasoning = None
        widget = ToolCallEntry(event.call_id, event.name, event.args)
        self._tool_widgets[event.call_id] = widget
        self._mount_entry(widget)
        self._set_pet(PetState.WORKING)

    def _handle_tool_end(self, event: ToolEnd) -> None:
        widget = self._tool_widgets.pop(event.call_id, None)
        if widget is not None:
            widget.finish(event.result, event.duration)
        if event.name == "TodoWrite":
            self.query_one(TodoPanel).render_todos(self.agent.ctx.todos)
        self._transcript().scroll_end(animate=False)

    # -- approval ---------------------------------------------------------

    async def _approve(self, tool: str, args: dict[str, Any], verdict: Verdict) -> bool:
        """Ask the user to approve a tool call. Runs inside the turn worker."""
        choice = await self.push_screen_wait(PermissionModal(tool, args, verdict))
        if choice == "always" and verdict.suggested_rule:
            self.permissions.allow_persistently(verdict.suggested_rule)
            try:
                from ..config import save_config

                save_config(self.config)
            except OSError:
                pass
            self._notice(f"allowed and saved: {verdict.suggested_rule}")
        return choice in ("once", "always")

    async def confirm(self, title: str, detail: str = "") -> bool:
        """Ask the user to confirm an irreversible action."""
        return bool(await self.push_screen_wait(ConfirmModal(title, detail)))

    async def _ask_user(self, questions: list[Any]) -> dict[str, str]:
        """Put clarifying questions to the user and return their answers."""
        answers = await self.push_screen_wait(QuestionModal(questions))
        if answers:
            rendered = ", ".join(f"{k}: {v}" for k, v in answers.items())
            self._notice(f"answered — {rendered}")
        return answers or {}

    async def _present_plan(self, plan: Any) -> tuple[bool, str]:
        """Show a plan and wait for approval or feedback."""
        self.plan = plan
        self._show_plan(plan)
        approved, feedback = await self.push_screen_wait(PlanModal(plan, self.chinese))
        if approved:
            self.set_plan_mode(False)
            plan.approved = True
            self._notice("plan approved — writes unblocked", "info")
        else:
            self._notice("plan sent back for revision", "warn")
        return approved, feedback

    def _show_plan(self, plan: Any) -> None:
        panel = self.query_one(PlanPanel)
        panel.render_plan(plan, chinese=self.chinese)

    # -- plan mode --------------------------------------------------------

    def set_plan_mode(self, active: bool) -> None:
        """Enter or leave plan mode.

        In plan mode the permission engine refuses every write, regardless of
        the permission mode — including yolo. That is enforced in code rather
        than by instructing the model, because a model convinced its plan is
        correct will otherwise start on it.
        """
        self.plan_mode = active
        self.permissions.set_plan_mode(active)
        self.agent.invalidate_system_prompt()
        if not active:
            self.query_one(PlanPanel).remove_class("visible")
        self._refresh_status()

    def set_explore_mode(self, active: bool) -> None:
        """Enter or leave read-only explore mode."""
        self.permissions.set_explore_mode(active)
        self.plan_mode = self.permissions.plan_mode
        self.agent.invalidate_system_prompt()
        if active:
            self.query_one(PlanPanel).remove_class("visible")
        self._refresh_status()

    # -- mascot -----------------------------------------------------------

    def _set_pet(self, state: PetState) -> None:
        if not self.prefs.pet:
            return
        if self.mascot.set_state(state):
            self._refresh_pet()

    def _refresh_pet(self) -> None:
        try:
            panel = self.query_one(PetPanel)
        except Exception:  # noqa: BLE001 - not mounted yet
            return
        panel.display = self.prefs.pet and self.prefs.pet_style != "off"
        if panel.display:
            panel.refresh_pet()

    def _doze(self) -> None:
        """Send the mascot to sleep after a long idle stretch."""
        if not self._busy:
            self._set_pet(PetState.SLEEPING)

    def set_pet(self, *, enabled: bool | None = None, style: str | None = None) -> None:
        """Turn the mascot on/off or change how it is drawn."""
        if enabled is not None:
            self.prefs.pet = enabled
        if style is not None:
            self.prefs.pet_style = style
            self.mascot.style = style
        self.prefs.save()
        self._refresh_pet()

    # -- theme ------------------------------------------------------------

    def set_theme_named(self, name: str) -> bool:
        """Switch colour scheme and remember the choice.

        Args:
          name: A theme name from :data:`aiharness.ui.theme.THEMES`.

        Returns:
          True when the theme existed and was applied.
        """
        spec = get_theme(name)
        if spec is None:
            return False
        self.theme = spec.name
        self.prefs.theme = spec.name
        self.prefs.save()
        return True

    def show_context_breakdown(self) -> None:
        """Render the breakdown panel, used by /context and ctrl+g."""
        self.query_one(ContextPanel).render_breakdown(
            self.agent.context_breakdown(), chinese=self.chinese
        )

    def action_toggle_context(self) -> None:
        """Show or hide the context breakdown."""
        panel = self.query_one(ContextPanel)
        if panel.has_class("visible"):
            panel.hide()
        else:
            panel.render_breakdown(self.agent.context_breakdown(), chinese=self.chinese)

    def action_cycle_theme(self) -> None:
        names = list(THEMES)
        current = self.prefs.theme if self.prefs.theme in names else names[0]
        nxt = names[(names.index(current) + 1) % len(names)]
        self.set_theme_named(nxt)
        self._notice(f"theme: {THEMES[nxt].label}")

    # -- state changes used by commands -----------------------------------

    def set_selection(self, selection: Selection) -> None:
        self.agent.set_selection(selection)
        self._refresh_status()

    def _apply_session_permission_mode(self) -> None:
        mode = self.session.meta.permission_mode or self.config.permissions.mode
        if mode not in ("ask", "auto", "yolo"):
            mode = "ask"
        if self.permissions.mode != mode:
            self.permissions.set_mode(mode)
            self.agent.invalidate_system_prompt()

    def start_new_session(self) -> None:
        binding = self.config.role("main")
        self.session = self.sessions.create(
            self.workspace,
            model=binding.model if binding else "",
            account=(binding.account or "") if binding else "",
            permission_mode=self.permissions.mode,
        )
        self.agent = self._build_agent()
        self._wire_context()
        self._apply_session_permission_mode()
        self._transcript().remove_children()
        self.query_one(TodoPanel).render_todos([])
        self._refresh_status()
        self._refresh_sidebar()

    def resume_session(self, session_id: str) -> bool:
        handle = self.sessions.open(session_id)
        if handle is None:
            return False
        self.session = handle
        self.agent = self._build_agent()
        self._wire_context()
        self._apply_session_permission_mode()
        self._transcript().remove_children()
        self._restore_transcript()
        self._refresh_status()
        self._refresh_sidebar()
        return True

    def clear_conversation(self) -> None:
        self.agent.clear()
        self._transcript().remove_children()
        self.query_one(TodoPanel).render_todos([])
        self._notice("conversation erased")
        self._refresh_status()

    def reload_skills(self) -> None:
        self.skills = SkillLibrary(self.workspace, self.config.skill_paths).load()
        self.agent.skills = self.skills
        self.agent.ctx.skills = self.skills

    # -- scheduler --------------------------------------------------------

    def ensure_scheduler(self) -> None:
        """Start the background scheduler if any job is enabled."""
        if not any(job.enabled for job in self.jobs.all()):
            return
        if self.scheduler is None:
            self.scheduler = Scheduler(
                self.config, self.router, self.jobs, sessions=self.sessions,
                on_run=self._on_job_finished,
            )
        self.scheduler.start()

    def _on_job_finished(self, job: ScheduledJob, record: RunRecord) -> None:
        glyph = "✓" if record.ok else "✗"
        detail = record.error or (record.summary or "").splitlines()[:1]
        detail_text = detail if isinstance(detail, str) else (detail[0] if detail else "")
        self._notice(
            f"{glyph} scheduled job '{job.name}' finished in {record.duration:.0f}s "
            f"(${record.cost:.4f}) — {detail_text[:160]}",
            "info" if record.ok else "warn",
        )
        self._refresh_sidebar()

    @work
    async def run_job_now(self, job_id: str) -> None:
        self.ensure_scheduler()
        if self.scheduler is None:
            self.scheduler = Scheduler(
                self.config, self.router, self.jobs, sessions=self.sessions,
                on_run=self._on_job_finished,
            )
        await self.scheduler.run_now(job_id)

    # -- chrome -----------------------------------------------------------

    def _refresh_status(self) -> None:
        model = self.config.model(self.agent.selection.model_id)
        effort = self.agent.selection.effort or (model.default_effort if model else "-")
        if model is not None and not model.effort_levels():
            effort = "no-effort"
        due = sum(1 for job in self.jobs.all() if job.enabled)
        self.query_one(StatusBar).render_status(
            model=self.agent.selection.model_id or "(none)",
            account=self.agent.selection.account_id or "",
            effort=effort,
            mode="PLAN" if self.plan_mode else self.permissions.mode,
            used=self.agent.context_used(),
            window=self.agent.context_window(),
            cache_hit=self.agent.state.cache.hit_rate,
            cost=self.router.ledger.total_cost,
            jobs=due,
            busy=self._busy,
        )

    def _refresh_sidebar(self) -> None:
        entries = self.sessions.list(workspace=self.workspace, limit=10)
        lines = [
            ("▸ " if meta.id == self.session.meta.id else "  ")
            + f"{meta.title or meta.id}"
            for meta in entries
        ] or ["  (none)"]
        self.query_one("#sidebar-sessions", Static).update("\n".join(lines))

        job_lines = [
            f"  {'on ' if job.enabled else 'off'} {job.name} — {job.next_run_label()}"
            for job in self.jobs.all()[:10]
        ] or ["  (none)"]
        self.query_one("#sidebar-jobs", Static).update("\n".join(job_lines))

    # -- actions ----------------------------------------------------------

    def action_toggle_sidebar(self) -> None:
        sidebar = self.query_one("#sidebar")
        sidebar.toggle_class("visible")
        self.prefs.sidebar_visible = sidebar.has_class("visible")
        self.prefs.save()
        self._refresh_pet()

    def action_toggle_reasoning(self) -> None:
        self._show_reasoning = not self._show_reasoning
        self.prefs.show_reasoning = self._show_reasoning
        self.prefs.save()
        self._notice(f"reasoning display {'on' if self._show_reasoning else 'off'}")

    def action_clear_screen(self) -> None:
        """Clear the visible transcript without touching the conversation."""
        self._transcript().remove_children()
        self._notice("screen cleared (conversation intact — /clear erases it)")

    def action_interrupt(self) -> None:
        if self._busy:
            self.agent.interrupt()
            self.workers.cancel_group(self, "default")
            self._notice("interrupting…", "warn")
        else:
            self._notice("nothing running (ctrl+d to quit)")

    @work
    async def connect_mcp(self) -> None:
        """Connect to the configured MCP servers and publish their tools."""
        statuses = await self.mcp.connect_all()
        for status in statuses:
            if status.connected:
                self._notice(
                    f"mcp: {status.id} connected, {status.tool_count} tool(s)"
                )
            else:
                self._notice(f"mcp: {status.id} unavailable — {status.error}", "warn")
        self.rebuild_tools()

    def rebuild_tools(self) -> None:
        """Rebuild the agent's registry, e.g. after MCP servers connect."""
        self.agent.tools = self._build_tool_registry()

    async def action_quit(self) -> None:
        self.heartbeat.stop(StopReason.USER_STOPPED)
        if self.scheduler is not None:
            await self.scheduler.stop()
        if getattr(self.agent.ctx, "browser", None) is not None:
            await self.agent.ctx.browser.close()
        await self.mcp.close()
        await self.router.aclose()
        self.exit()
