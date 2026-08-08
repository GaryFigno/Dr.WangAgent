"""Handling the commands the frontend sends.

Split from :mod:`aiharness.gui.bridge` so the session class stays about
lifecycle and event translation, and this stays about "the user clicked
something". Each handler is small, returns nothing, and pushes whatever the
frontend needs to re-render.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from ..agent.heartbeat import NO_LIMIT, HeartbeatLimits, StopReason
from ..agent.loop import Compacted, Done, Notice, Text, Thinking, ToolEnd, ToolStart, TurnEnd
from ..config.loader import save_config
from ..constants import HEARTBEAT_DEFAULT_INTERVAL, QUEST_STEP_MAX_RETRIES
from ..credentials import CredentialStore, classify
from ..providers import proxy
from ..providers.router import NoRouteError, Selection
from ..setup import (
    SetupError,
    assign_role,
    build_account,
    build_model,
    probe_and_list,
    suggest_alias,
)
from ..skills import SkillLibrary
from .bridge import GuiSession, tool_headline
from .protocol import Inbound, Outbound

#: Permission modes the frontend may set.
VALID_MODES = ("ask", "auto", "yolo")


def open_todos_remaining(todos: list[dict] | None) -> list[dict]:
    """TodoWrite items that are not completed."""
    return [
        item
        for item in (todos or [])
        if str(item.get("status") or "") != "completed"
    ]


def _todo_continue_prompt(todos: list[dict]) -> str:
    lines = []
    for item in open_todos_remaining(todos):
        status = str(item.get("status") or "pending")
        mark = ">" if status == "in_progress" else "-"
        lines.append(f"{mark} [{status}] {item.get('content', '')}")
    body = "\n".join(lines) or "(no open todos)"
    return (
        "[Continue unfinished work]\n"
        "Open todos:\n"
        f"{body}\n"
        "Continue from the in-progress / next pending item. "
        "Do not restart completed work. Update TodoWrite as you go."
    )


async def _persist_config(session: GuiSession) -> bool:
    """Write the in-memory config to disk.

    Setup used to require a separate "保存配置" click after every account or
    model change. People added a second Kimi key, saw it in the UI, quit, and
    found nothing on the next launch — the form had succeeded, the file had
    not. Structural edits now save themselves.

    Incomplete setup (no ``main`` role yet, roles still pointing at a model
    the user is about to add) must not block the write — that is the normal
    first-run path. Only contradictions that would corrupt the file stop it.
    """
    problems = [
        problem
        for problem in session.config.validate()
        if problem != "no 'main' role configured"
        and not problem.startswith("role '")
    ]
    if problems:
        await session.push(
            Outbound.ERROR, message="未能保存配置：" + "; ".join(problems)
        )
        return False
    path = save_config(session.config)
    await session.push(Outbound.NOTICE, level="info", text=f"已保存 {path}")
    return True


async def dispatch(session: GuiSession, command: Inbound, args: dict[str, Any]) -> None:
    """Route one frontend command to its handler.

    Unknown commands are impossible here: :func:`~.protocol.parse_inbound`
    has already rejected anything not in the enum.
    """
    handler = HANDLERS.get(command)
    if handler is None:  # pragma: no cover - the enum keeps this unreachable
        await session.push(Outbound.ERROR, message=f"unhandled command {command.value}")
        return
    try:
        await handler(session, args)
    except Exception as error:  # noqa: BLE001 - a bad click must not kill the socket
        await session.push(
            Outbound.ERROR, message=f"{type(error).__name__}: {error}"
        )


# --------------------------------------------------------------------------
# conversation
# --------------------------------------------------------------------------


async def _prompt(session: GuiSession, args: dict[str, Any]) -> None:
    from ..quest import quest_prompt_hint
    from ..session.attachments import AttachmentError, parse_inbound_images
    from ..workspace.paths import build_refs_block

    text = str(args.get("text", "")).strip()
    refs = [str(r).strip().lstrip("@") for r in (args.get("refs") or []) if str(r).strip()]
    try:
        images = parse_inbound_images(args.get("images"))
    except AttachmentError as error:
        await session.push(Outbound.NOTICE, level="warn", text=str(error))
        return
    if not text and not images and not refs:
        return
    view_id = session.session.meta.id
    if view_id in session.live:
        await session.push(
            Outbound.NOTICE, level="warn", text=session.msg("busy")
        )
        return
    # Sending clears the draft — keeping it would resurrect the prompt after
    # the turn finishes and look like the send failed.
    session.drafts.clear(view_id)
    session.last_turn_refs = refs
    display_text = text
    ref_block, _ = build_refs_block(session.workspace, refs)
    quest_hint = quest_prompt_hint(session.workspace, session_id=view_id)
    wired = f"{quest_hint}{ref_block}{text}".strip()
    if not wired and images:
        wired = "(see attached images)"
    if session.armed_limits is not None and await _launch_heartbeat(session, wired or text):
        return
    # Claim the live slot before the task starts so a second prompt cannot
    # interleave and park a user turn between tool_calls and tool results.
    from .bridge import LiveTurn

    session.live[view_id] = LiveTurn(
        session_id=view_id,
        handle=session.session,
        agent=session.agent,
        task=None,
    )
    try:
        task = asyncio.create_task(
            run_turn(
                session,
                wired or text,
                images=images,
                display_text=display_text,
                refs=refs,
            )
        )
    except Exception:
        session.live.pop(view_id, None)
        raise
    session.live[view_id].task = task
    session._turn_task = task


async def run_turn(
    session: GuiSession,
    text: str,
    *,
    automatic: bool = False,
    images: list | None = None,
    display_text: str | None = None,
    refs: list[str] | None = None,
) -> str:
    """Run one turn, streaming every event to the frontend.

    Args:
      session: The live session.
      text: The prompt to run (may include @refs / quest hint).
      automatic: True when a heartbeat drove this, which suppresses the
        classification step so an automatic continuation is not re-scored.
      images: Ordered pasted images for this user turn.
      display_text: What to show in the transcript (defaults to ``text``).
      refs: @ paths attached on this turn (shown in the context panel).

    Returns:
      The agent's closing text, which the heartbeat inspects for its verdict.
    """
    from .bridge import LiveTurn

    final = ""
    agent = session.agent
    handle = session.session
    selection = agent.selection
    turn_refs = list(refs if refs is not None else session.last_turn_refs)
    # Pin ownership to this conversation. The user may open another chat
    # mid-turn; events keep flowing tagged with this id and the frontend
    # ignores them while looking elsewhere.
    turn_session_id = handle.meta.id
    live = LiveTurn(
        session_id=turn_session_id,
        handle=handle,
        agent=agent,
        task=asyncio.current_task(),
    )
    session.live[turn_session_id] = live
    session.stream_session_id = turn_session_id
    try:
        if not automatic:
            text = await _route_by_complexity(session, text, agent=agent)
        # Heal before the new user turn, same as Agent.run — otherwise an
        # interrupted tool_calls tail lands after this message.
        agent.seal_unanswered_tool_calls()
        # Persist the user turn before TURN_START so the sidebar shows the
        # chat (and its title) while the model is still thinking.
        notice = agent.add_user_message(text, images=images)
        session.note_activity(
            turn_session_id,
            f"{selection.label()} 思考中…" if selection.model_id else "思考中…",
        )
        await session.push(
            Outbound.TURN_START,
            text=display_text if display_text is not None else text,
            model=selection.model_id or "",
            account=selection.account_id or "",
            images=_transcript_images(session, images),
            refs=turn_refs,
            session_id=turn_session_id,
        )
        if notice:
            await session.push(
                Outbound.NOTICE,
                level="warn",
                text=notice,
                session_id=turn_session_id,
            )
        await session.push_sessions()
        await session.push_status()
        if session.session.meta.id == turn_session_id:
            await session.push_context()
        async for event in agent.run(None):
            final = await _forward(session, event, session_id=turn_session_id) or final
    except asyncio.CancelledError:
        # Hard cancel can land between an assistant tool_calls message and
        # its tool results; seal before the next prompt hits the provider.
        agent.seal_unanswered_tool_calls()
        # Soft interrupt already yields Done; hard cancel must still emit a
        # terminal DONE so the UI does not stick on "正在打断…".
        if not live.done_sent:
            live.done_sent = True
            await session.push(
                Outbound.NOTICE,
                level="warn",
                text=session.msg("interrupted"),
                session_id=turn_session_id,
            )
            await session.push(
                Outbound.DONE,
                text=final,
                interrupted=True,
                session_id=turn_session_id,
            )
        raise
    finally:
        agent.seal_unanswered_tool_calls()
        session.live.pop(turn_session_id, None)
        if session.stream_session_id == turn_session_id:
            session.stream_session_id = None
        await _drain_router_notices(session, session_id=turn_session_id)
        await session.push_status()
        await session.push_sessions()
        if turn_session_id not in session.live:
            await _schedule_post_turn_continue(session, turn_session_id)
    return final


async def _schedule_post_turn_continue(
    session: GuiSession, session_id: str
) -> None:
    """Quest verify retry, optional auto-continue, or a resume notice."""
    if session_id in session.live:
        return
    retry = session._quest_retry_after.pop(session_id, None)
    if retry and session.session.meta.id == session_id:
        session._turn_task = asyncio.create_task(
            run_turn(session, retry, automatic=True)
        )
        return

    todos = list(
        session.session_todos.get(session_id)
        or (
            session.agent.ctx.todos
            if session.session.meta.id == session_id
            else None
        )
        or session.session.todos
        or []
    )
    open_items = open_todos_remaining(todos)
    if not open_items:
        session._auto_continues.pop(session_id, None)
        return

    ui = session.config.ui
    used = session._auto_continues.get(session_id, 0)
    max_n = max(0, int(getattr(ui, "max_auto_continues", 3) or 0))
    if (
        ui.auto_continue_open_todos
        and used < max_n
        and session.session.meta.id == session_id
        and not session._pending
    ):
        session._auto_continues[session_id] = used + 1
        prompt = _todo_continue_prompt(todos)
        await session.push(
            Outbound.NOTICE,
            level="info",
            text=session.msg(
                "todo.auto_continue", n=used + 1, max=max_n
            ),
            session_id=session_id,
        )
        session._turn_task = asyncio.create_task(
            run_turn(
                session,
                prompt,
                automatic=True,
                display_text=f"[Auto-continue {used + 1}/{max_n}]",
            )
        )
        return

    if session.session.meta.id == session_id:
        await session.push(
            Outbound.NOTICE,
            level="info",
            text=session.msg("todo.resume_hint", n=len(open_items)),
            session_id=session_id,
        )


def _transcript_images(session: GuiSession, images: list | None) -> list[dict]:
    """Placeholder for TURN_START; real thumbs come from the client optimistic UI."""
    if not images:
        return []
    return [{"name": image.name, "mime": image.mime} for image in images]


async def _drain_router_notices(
    session: GuiSession, *, session_id: str = ""
) -> None:
    """Report workarounds the router applied on its own.

    Dropping a parameter an endpoint rejected rescues the turn, but doing it
    silently means the next person to read the config cannot tell why the
    model behaves differently than it is configured to.
    """
    sid = session_id or session.stream_session_id or session.session.meta.id
    while session.router.notices:
        await session.push(
            Outbound.NOTICE,
            level="warn",
            text=session.router.notices.pop(0),
            session_id=sid,
        )


async def _forward(
    session: GuiSession, event: Any, *, session_id: str = ""
) -> str | None:
    """Translate one agent event into a frontend message."""
    sid = session_id or session.session.meta.id

    async def _push(outbound: Outbound, **payload: Any) -> None:
        await session.push(outbound, session_id=sid, **payload)

    if isinstance(event, Text):
        live = session.live.get(sid)
        who = live.agent.selection if live else None
        session.note_activity(
            sid,
            f"{who.label()} 回答中…" if who and who.model_id else "回答中…",
        )
        await _push(
            Outbound.TEXT,
            delta=event.text,
            model=(who.model_id if who else ""),
            account=(who.account_id if who else "") or "",
        )
    elif isinstance(event, Thinking):
        live = session.live.get(sid)
        who = live.agent.selection if live else None
        session.note_activity(
            sid,
            f"{who.label()} 思考中…" if who and who.model_id else "思考中…",
        )
        await _push(
            Outbound.THINKING,
            delta=event.text,
            model=(who.model_id if who else ""),
            account=(who.account_id if who else "") or "",
        )
    elif isinstance(event, ToolStart):
        headline = tool_headline(event.name, event.args)
        session.note_activity(sid, headline)
        await _push(
            Outbound.TOOL_START,
            call_id=event.call_id,
            name=event.name,
            args=event.args,
            headline=headline,
        )
    elif isinstance(event, ToolEnd):
        display = event.result.display or {}
        await _push(
            Outbound.TOOL_END,
            call_id=event.call_id,
            name=event.name,
            summary=event.result.summary,
            content=event.result.content,
            is_error=event.result.is_error,
            duration=event.duration,
            display=display,
        )
        if event.name == "TodoWrite":
            # Prefer the turn's own list (display / live agent). After the user
            # switches chats, session.agent is the *viewed* agent — reading it
            # would publish the wrong todos into the strip.
            live = session.live.get(sid)
            todos = list(
                display.get("todos")
                or (live.agent.ctx.todos if live else None)
                or session.agent.ctx.todos
                or []
            )
            handle = live.handle if live else session.session
            handle.save_todos(todos)
            session.session_todos[sid] = list(todos)
            if not open_todos_remaining(todos):
                session._auto_continues.pop(sid, None)
            await session.push(Outbound.TODOS, todos=todos, session_id=sid)
            from ..quest import sync_quest_from_todos

            quest = sync_quest_from_todos(
                session.workspace, todos, session_id=sid
            )
            if quest is not None:
                await session.push(
                    Outbound.QUEST, quest=quest.public(), session_id=sid
                )
        if event.name == "Verify":
            from ..quest import quest_prompt_hint, sync_quest_from_verify

            quest = sync_quest_from_verify(
                session.workspace,
                verdict=str(display.get("verdict") or ""),
                failures=int(display.get("failures") or 0),
                session_id=sid,
            )
            if quest is not None:
                await session.push(
                    Outbound.QUEST, quest=quest.public(), session_id=sid
                )
                if quest.status == "blocked":
                    await _push(
                        Outbound.NOTICE,
                        level="warn",
                        text=session.msg(
                            "quest.blocked",
                            reason=quest.blocked_reason or "Verify",
                        ),
                    )
                elif quest.retry_pending:
                    step = next(
                        (s for s in quest.steps if s.status == "active"), None
                    )
                    attempts = getattr(step, "attempts", 0) or 0
                    title = step.title if step else quest.goal
                    await _push(
                        Outbound.NOTICE,
                        level="info",
                        text=session.msg(
                            "quest.retrying",
                            n=attempts,
                            max=QUEST_STEP_MAX_RETRIES,
                        ),
                    )
                    # Schedule after this turn's finally — mid-tool ``busy``
                    # is always true, so starting another run_turn here never ran.
                    prompt = (
                        f"{quest_prompt_hint(session.workspace, session_id=sid)}"
                        f"Continue the active Quest from step: {title}. "
                        f"The last Verify failed; fix the root cause then "
                        f"re-verify."
                    )
                    session._quest_retry_after[sid] = prompt.strip()
        if event.name == "Bash" and not event.result.is_error:
            n = int(display.get("side_effects") or 0)
            board = session.edit_board(sid)
            if n and board.pending():
                await session.push_edit_review(session_id=sid)
                await _push(
                    Outbound.NOTICE,
                    level="info",
                    text=session.msg("bash.review", n=n),
                )
        if event.name in {"Edit", "Write"} and not event.result.is_error:
            from ..workspace.paths import invalidate_path_index

            invalidate_path_index(session.workspace)
            board = session.edit_board(sid)
            auto = (
                session.permissions.mode == "yolo"
                and session.config.ui.auto_apply_edits
            )
            if auto and board.pending():
                board.apply_all()
                await _push(
                    Outbound.NOTICE,
                    level="info",
                    text=session.msg("yolo.auto_apply"),
                )
            await session.push_edit_review(session_id=sid)
        path = str(display.get("path") or "")
        kind = str(display.get("kind") or "")
        if path and kind in {"write", "screenshot", "edit"} and _looks_canvas_path(path):
            await _push(Outbound.CANVAS_HINT, path=path, kind=kind)
    elif isinstance(event, Compacted):
        await _push(
            Outbound.COMPACTED,
            summary=event.summary,
            before=event.tokens_before,
            after=event.tokens_after,
            replaced=event.replaced,
        )
    elif isinstance(event, Notice):
        await _push(Outbound.NOTICE, level=event.level, text=event.text)
    elif isinstance(event, TurnEnd):
        await _push(
            Outbound.TURN_END,
            cost=event.cost,
            input_tokens=event.usage.input_tokens,
            output_tokens=event.usage.output_tokens,
            cached=event.usage.cached_tokens,
        )
        await session.push_status()
    elif isinstance(event, Done):
        live = session.live.get(sid)
        if live is not None and live.done_sent:
            return event.text
        if live is not None:
            live.done_sent = True
        if event.interrupted:
            await _push(
                Outbound.NOTICE, level="warn", text=session.msg("interrupted")
            )
        elif not (event.text or "").strip():
            # Tools-only / empty model replies used to clear the dock with no
            # visible answer — look like the turn vanished mid-thought.
            await _push(
                Outbound.NOTICE,
                level="warn",
                text=session.msg("turn.empty"),
            )
        await _push(Outbound.DONE, text=event.text, interrupted=event.interrupted)
        return event.text
    return None


def _classifier_binding(session: GuiSession):
    """Resolve the model that scores requests, preferring a cheap role.

    Falls through ``classifier_role`` → ``cheap`` → ``fast``. Using ``main``
    as a silent fallback made every greeting look like a project: the big
    model over-estimates ceremony. No cheap role means we skip classification.
    """
    planning = session.config.planning
    for role in (planning.classifier_role, "cheap", "fast"):
        binding = session.config.role(role)
        if binding is not None:
            return binding
    return None


async def _route_by_complexity(
    session: GuiSession, text: str, *, agent: Any = None
) -> str:
    """Score the request and enter plan mode when it is a project."""
    from ..agent.planning import build_classifier_context, classify_request

    owner = agent or session.agent
    sid = owner.session.meta.id if owner.session else session.session.meta.id
    planning = session.config.planning
    if not (planning.enabled and planning.auto_classify) or owner.permissions.plan_mode:
        return text
    binding = _classifier_binding(session)
    if binding is None:
        if not getattr(session, "_warned_no_classifier", False):
            session._warned_no_classifier = True
            await session.push(
                Outbound.NOTICE,
                level="warn",
                text=(
                    "未配置 cheap / fast / classifier 角色，已跳过自动 Plan 分类。"
                    "请在设置里给便宜模型绑定 cheap 或 fast 角色。"
                ),
                session_id=sid,
            )
        return text

    # Score against the open thread — isolated prompts over-enter plan mode.
    context = build_classifier_context(owner.messages)
    verdict = await classify_request(
        text,
        session.router,
        Selection.from_binding(binding),
        context=context,
    )
    await session.push(
        Outbound.NOTICE,
        level="info",
        text=f"{verdict.complexity.label_zh}（{verdict.score}/10）"
        + (f" — {verdict.reason}" if verdict.reason else ""),
        session_id=sid,
    )

    if verdict.needs_clarification and planning.ask_when_unclear:
        answers = await session._ask_questions(verdict.questions, session_id=sid)
        if answers:
            rendered = "\n".join(f"- {k}: {v}" for k, v in answers.items())
            text += f"\n\n<clarifications>\n{rendered}\n</clarifications>"

    # Small tasks do not need Research/Orchestrate cluttering the schema.
    owner.set_tool_profile("full" if verdict.needs_plan else "lite")

    if verdict.needs_plan and planning.require_plan_approval:
        owner.permissions.set_plan_mode(True)
        owner.invalidate_system_prompt()
        await session.push_status()
        await session.push(
            Outbound.NOTICE,
            level="info",
            text="已进入 Plan 模式：先调研再出方案，写入被拦住。"
            "点顶栏 PLAN 可退出；批准方案后也会自动退出。",
            session_id=sid,
        )
        text += (
            "\n\n[Plan mode is active. Writes are blocked. Investigate, then "
            "call PresentPlan. Do not edit anything until the user approves. "
            "AskUser questions/options and the plan text must use the same "
            "language as the user's messages.]"
        )
    return text


async def _steer(session: GuiSession, args: dict[str, Any]) -> None:
    """Inject guidance into the viewed chat's live turn (between model gaps)."""
    text = str(args.get("text", "")).strip()
    if not text:
        return
    view_id = session.session.meta.id
    live = session.live.get(view_id)
    if live is None:
        # Nothing running — treat as a normal prompt.
        await _prompt(session, args)
        return
    if not live.agent.steer(text):
        return
    await session.push(
        Outbound.NOTICE,
        level="info",
        text=session.msg("steer.queued", preview=text[:80]),
        session_id=view_id,
    )


async def _interrupt(session: GuiSession, args: dict[str, Any]) -> None:
    """Interrupt the viewed conversation’s in-flight turn (not every live one).

    First click is a soft interrupt so the agent can yield ``Done`` cleanly.
    A second click (or force) escalates to ``task.cancel()``.
    """
    view_id = session.session.meta.id
    live = session.live.get(view_id)
    if live is None:
        await session.push(
            Outbound.NOTICE, level="info", text=session.msg("interrupt.idle")
        )
        return
    already = live.agent._cancel.is_set()
    live.agent.interrupt()
    force = bool(args.get("force"))
    if (already or force) and live.task is not None and not live.task.done():
        live.hard_cancel = True
        live.task.cancel()
    # Activity line only — a transcript notice stuck on "正在打断…" forever
    # looked like the interrupt never finished while tools kept running.
    await session.push(
        Outbound.ACTIVITY,
        text=session.msg("interrupting"),
        session_id=view_id,
    )


async def _answer(session: GuiSession, args: dict[str, Any]) -> None:
    session.resolve(str(args.get("id", "")), args.get("answers"))


async def _approve(session: GuiSession, args: dict[str, Any]) -> None:
    session.resolve(str(args.get("id", "")), args.get("decision"))


async def _plan_decision(session: GuiSession, args: dict[str, Any]) -> None:
    session.resolve(
        str(args.get("id", "")),
        {"approved": args.get("approved"), "feedback": args.get("feedback", "")},
    )


# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------


async def _guard_view_idle(session: GuiSession) -> bool:
    """Refuse actions that need the *viewed* chat to be idle (e.g. rewind)."""
    if session.session.meta.id not in session.live:
        return True
    await session.push(Outbound.NOTICE, level="warn", text=session.msg("busy"))
    return False


async def _new_session(session: GuiSession, args: dict[str, Any]) -> None:
    """Start a fresh conversation, optionally in another project.

    The project is chosen here rather than from a global picker, because a
    conversation belongs to one tree for its whole life; switching the tree
    underneath an existing conversation would silently change what the agent
    is allowed to touch.

    A turn already running in the previous chat keeps going in the background.
    """
    requested = str(args.get("path", "")).strip()
    if requested:
        target = Path(requested).expanduser().resolve()
        if target != session.workspace:
            try:
                session.point_workspace(target)
            except NotADirectoryError as error:
                await session.push(Outbound.ERROR, message=str(error))
                return
            session.last_turn_refs = []

    # Opening a conversation in a directory is what makes it a project. Doing
    # this unconditionally matters for the directory the app is already in:
    # the branch above is skipped for it, so picking its chip after removing
    # it used to leave the sidebar empty with no way to get the project back.
    session.remember_workspace(session.workspace)
    await _start_fresh_session(session)
    await session.push_workspace(session.live_workspaces())


def _reset_plan_mode(session: GuiSession) -> None:
    """Plan/explore modes are per conversation, not global sticky switches.

    Permissions live on the GuiSession for the whole app lifetime, so without
    this a single project-sized request left the PLAN badge on every new chat.
    """
    changed = False
    if session.permissions.plan_mode:
        session.permissions.set_plan_mode(False)
        changed = True
    if session.permissions.explore_mode:
        session.permissions.set_explore_mode(False)
        changed = True
    if changed:
        session.agent.invalidate_system_prompt()
    session.plan = None


def _apply_session_permission_mode(session: GuiSession) -> None:
    """Restore ask/auto/yolo for the open chat (meta, else config default)."""
    mode = session.session.meta.permission_mode or session.config.permissions.mode
    if mode not in VALID_MODES:
        mode = session.config.permissions.mode
    if mode not in VALID_MODES:
        mode = "ask"
    if session.permissions.mode != mode:
        session.permissions.set_mode(mode)
        session.agent.invalidate_system_prompt()


async def _start_fresh_session(session: GuiSession) -> None:
    """Swap in a new empty conversation using the config default model.

    The dialog picker is per-session. A brand-new chat falls back to
    ``roles.main`` (seeded on the handle); it does not inherit the previous
    chat's selection. Separate from :func:`_new_session`, which also
    registers the directory as a project.

    A background turn keeps its own Agent; only the view pointer moves.
    """
    session.edit_review.clear()
    session.session = session._new_session_handle()
    session.agent = session._build_agent()
    session._wire_context()
    _apply_session_permission_mode(session)
    _reset_plan_mode(session)
    await session.push_transcript()
    await session.push_all()
    await session.push_edit_review()
    await session.push_pending_hitl()


async def _open_session(session: GuiSession, args: dict[str, Any]) -> None:
    """Open a conversation; a turn already running elsewhere keeps going."""
    target_id = str(args.get("id", ""))
    if target_id == session.session.meta.id and target_id not in session.live:
        return
    live = session.live.get(target_id)
    if live is not None:
        handle = live.handle
        agent = live.agent
    else:
        handle = session.sessions.open(target_id)
        agent = None
    if handle is None:
        await session.push(Outbound.ERROR, message="没有这个会话")
        return
    # Open across projects atomically — do not create an empty session in the
    # destination first (that raced live streams onto the wrong transcript).
    home = Path(handle.meta.workspace)
    if home.resolve() != session.workspace.resolve():
        try:
            session.point_workspace(home)
        except NotADirectoryError as error:
            await session.push(Outbound.ERROR, message=str(error))
            return
        session.last_turn_refs = []
    session.session = handle
    session.agent = agent if agent is not None else session._build_agent()
    session._wire_context()
    if live is None:
        _apply_session_permission_mode(session)
        _reset_plan_mode(session)
    await session.push_workspace(session.live_workspaces())
    await session.push_transcript()
    await session.push_all()
    await session.push_edit_review()
    await session.push_pending_hitl()
    # Re-assert activity for a background turn the user just opened, so the
    # sticky dock is not left empty after clear-on-switch.
    if live is not None and live.last_activity:
        await session.push(
            Outbound.ACTIVITY,
            text=live.last_activity,
            source="main",
            session_id=target_id,
        )
    if live is None:
        await _warn_if_interrupted_session(session)


async def _delete_session(session: GuiSession, args: dict[str, Any]) -> None:
    """Delete a session, then land somewhere sensible.

    Deleting the session you are looking at used to create a fresh empty one
    immediately, which is indistinguishable from the row you just deleted —
    so it looked like nothing happened. Now it opens the next surviving
    session instead, and only starts a new one when there is nothing left.
    """
    target = str(args.get("id", ""))
    live = session.live.pop(target, None)
    if live is not None:
        live.agent.interrupt()
        if live.task is not None:
            live.task.cancel()
    if not session.sessions.delete(target):
        await session.push(Outbound.ERROR, message="那个会话已经不在了")
        await session.push_sessions()
        return
    session.drafts.clear(target)
    session.drop_edit_board(target)
    session._quest_retry_after.pop(target, None)
    session.session_todos.pop(target, None)

    if target != session.session.meta.id:
        await session.push_sessions()
        return

    remaining = session.sessions.list(
        workspace=session.workspace, limit=1, include_empty=False
    )
    if remaining:
        await _open_session(session, {"id": remaining[0].id})
    else:
        await _start_fresh_session(session)


async def _archive_session(session: GuiSession, args: dict[str, Any]) -> None:
    """Hide a session without destroying it."""
    target = str(args.get("id", ""))
    archived = bool(args.get("archived", True))
    if not session.sessions.set_archived(target, archived):
        await session.push(Outbound.ERROR, message="没有这个会话")
        return

    if archived and target == session.session.meta.id:
        remaining = session.sessions.list(
            workspace=session.workspace, limit=1, include_empty=False
        )
        if remaining:
            await _open_session(session, {"id": remaining[0].id})
            return
        await _start_fresh_session(session)
        return
    await session.push_sessions()


async def _toggle_archived(session: GuiSession, args: dict[str, Any]) -> None:
    session.show_archived = bool(args.get("show", not session.show_archived))
    await session.push_sessions()


async def _clear_session(session: GuiSession, args: dict[str, Any]) -> None:
    session.agent.clear()
    session.edit_review.clear()
    await session.push_transcript()
    await session.push_all()
    await session.push_edit_review()


async def _rewind_turn(session: GuiSession, args: dict[str, Any]) -> None:
    """Drop a user turn and everything after it from the open conversation."""
    if not await _guard_view_idle(session):
        return
    try:
        user_index = int(args.get("user_index", -1))
    except (TypeError, ValueError):
        user_index = -1
    if user_index < 0:
        await session.push(
            Outbound.ERROR, message=session.msg("rewind.bad_index")
        )
        return

    history = session.session.full_history
    seen = -1
    cut: int | None = None
    for index, message in enumerate(history):
        if message.role != "user":
            continue
        seen += 1
        if seen == user_index:
            cut = index
            break
    if cut is None:
        await session.push(
            Outbound.ERROR, message=session.msg("rewind.missing")
        )
        return

    removed = session.session.truncate_from(cut)
    # Reload before sealing: sealing against the pre-truncate in-memory list
    # can append orphan tool results onto the already-cut session log.
    session.agent._messages = list(session.session.view())
    session.agent.seal_unanswered_tool_calls()
    # Restore disk from edit-review snapshots before dropping the board —
    # otherwise rewind only cuts the transcript and leaves writes behind.
    restored, restore_errors = session.edit_review.reject_all()
    _sync_read_files(session)
    session.edit_review.clear()
    await session.push(
        Outbound.NOTICE,
        level="info",
        text=session.msg("rewind.ok", n=removed),
    )
    if restored:
        await session.push(
            Outbound.NOTICE,
            level="info",
            text=session.msg("rewind.restored", n=restored),
        )
    for detail in restore_errors[:3]:
        await session.push(Outbound.NOTICE, level="warn", text=detail)
    await session.push_transcript()
    await session.push_all()
    await session.push_edit_review()


def _sync_read_files(session: GuiSession, path: Any = None) -> None:
    """Refresh mtimes after a reject so the next Edit sees current disk."""
    from pathlib import Path

    ctx = session.agent.ctx
    paths = [Path(path)] if path else [item.path for item in session.edit_review.items]
    for file_path in paths:
        key = str(file_path.resolve())
        if file_path.exists():
            try:
                ctx.read_files[key] = file_path.stat().st_mtime
            except OSError:
                ctx.read_files.pop(key, None)
        else:
            ctx.read_files.pop(key, None)


async def _edit_decision(session: GuiSession, args: dict[str, Any]) -> None:
    """Apply or reject pending Write/Edit snapshots (non-blocking review)."""
    action = str(args.get("action", "")).strip().lower()
    edit_id = str(args.get("id", "")).strip()
    board = session.edit_review
    touched: Any = None

    if action == "apply":
        ok, detail = board.apply(edit_id)
    elif action == "reject":
        item = board.get(edit_id)
        ok, detail = board.reject(edit_id)
        if ok and item is not None:
            touched = item.path
    elif action == "apply_all":
        count, detail = board.apply_all()
        ok = True
        detail = detail if count else "没有待审改动"
    elif action == "reject_all":
        count, errors = board.reject_all()
        ok = count > 0 or not errors
        detail = f"已回滚 {count} 处" + (
            ("；" + "；".join(errors[:3])) if errors else ""
        )
        if count == 0 and errors:
            ok = False
            detail = errors[0]
        if count > 0:
            _sync_read_files(session)
    else:
        await session.push(Outbound.ERROR, message=f"unknown edit action '{action}'")
        return

    if touched is not None:
        _sync_read_files(session, touched)
    level = "info" if ok else "warn"
    await session.push(Outbound.NOTICE, level=level, text=detail)
    await session.push_edit_review()


async def _compact(session: GuiSession, args: dict[str, Any]) -> None:
    event = await session.agent.compact_now()
    if event is None:
        await session.push(Outbound.NOTICE, level="info", text="nothing to compact yet")
        return
    await session.push(
        Outbound.COMPACTED,
        summary=event.summary,
        before=event.tokens_before,
        after=event.tokens_after,
        replaced=event.replaced,
    )
    await session.push_status()


async def _uncompact(session: GuiSession, args: dict[str, Any]) -> None:
    dropped = session.agent.restore_full_history()
    await session.push(
        Outbound.NOTICE,
        level="info",
        text=f"restored the full transcript ({dropped} compaction(s) undone)"
        if dropped
        else "nothing was compacted",
    )
    await session.push_status()


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------


async def _set_model(session: GuiSession, args: dict[str, Any]) -> None:
    # Mid-turn model swaps break providers that require reasoning_content
    # echo (DeepSeek thinking). Lock the selector until the turn finishes.
    view_id = session.session.meta.id
    if view_id in session.live:
        await session.push(
            Outbound.NOTICE, level="warn", text=session.msg("model.busy")
        )
        await session.push_status()
        return
    try:
        selection = Selection.parse(str(args.get("spec", "")), session.config)
    except NoRouteError as error:
        await session.push(Outbound.ERROR, message=str(error))
        return
    selection.effort = selection.effort or session.agent.selection.effort
    session.agent.set_selection(selection)
    await session.push_status()


async def _set_mode(session: GuiSession, args: dict[str, Any]) -> None:
    mode = str(args.get("mode", ""))
    if mode not in VALID_MODES:
        await session.push(Outbound.ERROR, message=f"unknown mode '{mode}'")
        return
    session.permissions.set_mode(mode)
    session.session.set_permission_mode(mode)
    # Last choice becomes the default for new chats after restart.
    session.config.permissions.mode = mode
    session.agent.invalidate_system_prompt()
    await session.push_status()
    await _persist_config(session)


async def _set_effort(session: GuiSession, args: dict[str, Any]) -> None:
    if session.session.meta.id in session.live:
        await session.push(
            Outbound.NOTICE, level="warn", text=session.msg("model.busy")
        )
        await session.push_status()
        return
    selection = session.agent.selection
    selection.effort = str(args.get("effort", "")) or None
    session.agent.set_selection(selection)
    await session.push_status()


async def _add_account(session: GuiSession, args: dict[str, Any]) -> None:
    """Add an account after checking it against the live endpoint.

    The credential field accepts either form, because insisting on an
    environment variable made the first minute of the app hostile: the key is
    in the clipboard, not in the environment, and "go set a variable and
    restart" is a poor answer to "I want to try this".

    A pasted key goes to the credential store, never into ``config.yaml``.
    """
    account_id = str(args.get("id", "")).strip()
    base_url = str(args.get("base_url", "")).strip()
    secret = str(args.get("credential", "") or args.get("env", "")).strip()

    if not account_id or not base_url or not secret:
        await _account_result(session, False, "三项都要填")
        return
    if session.config.account(account_id) is not None:
        await _account_result(session, False, f"账号名「{account_id}」已存在")
        return

    kind = classify(secret)
    if kind == "unknown":
        await _account_result(
            session,
            False,
            "看不出这是密钥还是环境变量名。密钥通常以 sk- 开头；"
            "环境变量名是全大写加下划线，例如 DEEPSEEK_API_KEY。",
        )
        return

    if kind == "env":
        key = os.environ.get(secret, "")
        if not key:
            await _account_result(
                session, False, f"这台机器上没有名为 {secret} 的环境变量"
            )
            return
        env_var = secret
    else:
        key, env_var = secret, ""

    try:
        account = build_account(account_id, base_url, key, api_key_env=env_var)
        account.proxy = proxy.normalise(str(args.get("proxy", "")))
    except (SetupError, proxy.ProxyError) as error:
        await _account_result(session, False, str(error))
        return

    await _account_result(session, None, f"正在验证 {account.base_url} …")
    probe = await probe_and_list(account)
    if not probe.ok:
        await _account_result(session, False, f"验证不通过，没有保存 —— {probe.detail}")
        return

    session.config.accounts.append(account)
    if not env_var:
        # Pasted keys live in their own file so the config stays shareable.
        CredentialStore().put(account_id, key)

    where = f"引用环境变量 {env_var}" if env_var else "密钥已存入独立的凭据文件（不在配置里）"
    await _account_result(
        session,
        True,
        f"已添加 **{account_id}** —— {probe.detail}；{where}。"
        + ("下一步：拉取模型列表。" if probe.chat_models else ""),
    )
    await session.push_config()
    await _persist_config(session)


async def _account_result(session: GuiSession, ok: bool | None, text: str) -> None:
    """Report the outcome inline in the form, not only as a toast.

    A toast that scrolls away while the form still shows the values the user
    typed reads as success. This keeps the verdict attached to the form.
    """
    await session.push(
        Outbound.CONFIG,
        account_result={"ok": ok, "text": text},
    )


async def _remove_account(session: GuiSession, args: dict[str, Any]) -> None:
    account = session.config.account(str(args.get("id", "")))
    if account is None:
        return
    CredentialStore().remove(account.id)
    users = [m.id for m in session.config.models if account.id in m.accounts]
    if users:
        await session.push(
            Outbound.ERROR,
            message=f"{account.id} still serves {', '.join(users)}",
        )
        return
    session.config.accounts.remove(account)
    await session.push_config()
    await _persist_config(session)


async def _list_account_models(session: GuiSession, args: dict[str, Any]) -> None:
    """Fetch what an account really serves, so only valid models are offered."""
    from ..setup import normalize_vision_mode

    account = session.config.account(str(args.get("id", "")))
    if account is None:
        await session.push(Outbound.ERROR, message="no such account")
        return
    probe = await probe_and_list(account)
    # Refresh auto-mode stamps when the vendor reports vision capability.
    changed = False
    if probe.ok and probe.vision:
        for model in session.config.models:
            if normalize_vision_mode(model.vision_mode) != "auto":
                continue
            if model.model not in probe.vision:
                continue
            flagged = bool(probe.vision[model.model])
            if model.supports_vision != flagged:
                model.supports_vision = flagged
                changed = True
    await session.push(
        Outbound.CONFIG,
        catalogue={
            "account": account.id,
            "ok": probe.ok,
            "detail": probe.detail,
            "models": probe.catalogue_rows(),
        },
    )
    if changed:
        await session.push_config()
        await _persist_config(session)


async def _add_model(session: GuiSession, args: dict[str, Any]) -> None:
    """Make a model available, through one more account if it already is.

    "One model, several accounts" is the whole shape of this config, but the
    add path ignored it: pulling ``k3`` from a second Kimi account invented a
    second definition called ``k3-2``, as though it were a different model.
    It is the same model — the account is what differs, and the picker
    already says so with ``k3@Kimi`` and ``k3@Kimi0018``.
    """
    account_id = str(args.get("account", ""))
    model_id = str(args.get("model", "")).strip()
    if session.config.account(account_id) is None:
        await session.push(Outbound.ERROR, message="no such account")
        return

    requested_alias = str(args.get("alias", "")).strip()
    twin = next((m for m in session.config.models if m.model == model_id), None)
    # An explicit alias means the user wants a separate entry on purpose —
    # same model, different settings. Only the automatic path merges.
    if twin is not None and (not requested_alias or requested_alias == twin.id):
        await _attach_account_to_model(session, twin, account_id)
        return

    existing = {m.id for m in session.config.models}
    alias = requested_alias or suggest_alias(model_id, existing)
    if alias in existing:
        await session.push(Outbound.ERROR, message=f"'{alias}' is already configured")
        return
    vision_hint = args.get("supports_vision", None)
    if vision_hint is None:
        # Re-probe once so adding from a stale catalogue still stamps vision.
        probe = await probe_and_list(session.config.account(account_id))
        if probe.ok and model_id in probe.vision:
            vision_hint = probe.vision[model_id]
    elif isinstance(vision_hint, str):
        vision_hint = vision_hint.strip().lower() in ("1", "true", "yes", "on")
    try:
        session.config.models.append(
            build_model(
                alias,
                model_id,
                [account_id],
                supports_vision=vision_hint if isinstance(vision_hint, bool) else None,
            )
        )
    except SetupError as error:
        await session.push(Outbound.ERROR, message=str(error))
        return
    await session.push_config()
    await _persist_config(session)


async def _set_model_vision(session: GuiSession, args: dict[str, Any]) -> None:
    """User override for whether a model accepts images: auto / on / off."""
    from ..session.attachments import infer_vision_capability, model_supports_vision
    from ..setup import normalize_vision_mode

    model = session.config.model(str(args.get("id", "")))
    if model is None:
        await session.push(Outbound.ERROR, message="no such model")
        return
    mode = normalize_vision_mode(str(args.get("mode", "auto")))
    model.vision_mode = mode
    # ``supports_vision`` stays a detection cache; overrides live in vision_mode.
    if mode == "auto" and not model.supports_vision:
        model.supports_vision = infer_vision_capability(model)
    label = {True: "看图", False: "纯文本"}[model_supports_vision(model)]
    mode_zh = {"auto": "自动", "on": "强制开", "off": "强制关"}[mode]
    await session.push(
        Outbound.NOTICE,
        level="info",
        text=f"{model.id} 看图：{label}（{mode_zh}）",
    )
    await session.push_config()
    await _persist_config(session)


async def _attach_account_to_model(session: GuiSession, model, account_id: str) -> None:
    """Add another account to a model that is already configured."""
    if account_id in model.accounts:
        await session.push(
            Outbound.NOTICE, level="info", text=f"{model.id} 已经在用 {account_id} 了"
        )
        return
    model.accounts.append(account_id)
    await session.push(
        Outbound.NOTICE,
        level="info",
        text=f"{model.id} 现在多了一个账号 {account_id}，选 {model.id}@{account_id} 即可",
    )
    await session.push_config()
    await _persist_config(session)


async def _remove_model(session: GuiSession, args: dict[str, Any]) -> None:
    model = session.config.model(str(args.get("id", "")))
    if model is None:
        return
    bound = [r for r, b in session.config.roles.items() if b.model == model.id]
    if bound:
        await session.push(
            Outbound.ERROR, message=f"{model.id} is still bound to {', '.join(bound)}"
        )
        return
    session.config.models.remove(model)
    await session.push_config()
    await _persist_config(session)


async def _set_role(session: GuiSession, args: dict[str, Any]) -> None:
    """Bind a role, refusing anything not configured.

    ``main`` is only the default for *new* sessions. Changing it must not
    hijack the open conversation — that follows the dialog model picker.
    """
    try:
        assign_role(
            session.config, str(args.get("role", "")), str(args.get("spec", ""))
        )
    except SetupError as error:
        await session.push(Outbound.ERROR, message=str(error))
        return
    await session.push_config()
    await session.push_status()
    await _persist_config(session)


async def _save_config(session: GuiSession, args: dict[str, Any]) -> None:
    await _persist_config(session)


async def _refresh(session: GuiSession, args: dict[str, Any]) -> None:
    from .workspace import RecentWorkspaces

    await session.push_all()
    # Replaying the transcript mid-turn wipes the live streaming bubble
    # (assistant text is not on disk until the model frame finishes). Skip
    # while busy; reconnect uses on_client_attached → push_transcript instead.
    force_transcript = bool(args.get("transcript") or args.get("force_transcript"))
    if force_transcript or not session.busy:
        await session.push_transcript()
    await session.push_context()
    await session.push_workspace(RecentWorkspaces.load().existing())
    await session.push_skills()


# --------------------------------------------------------------------------
# workspace and skills
# --------------------------------------------------------------------------


async def _pick_workspace(session: GuiSession, args: dict[str, Any]) -> None:
    """Open the OS folder picker, when a desktop window can host one."""
    from .workspace import native_folder_dialog

    chosen = await asyncio.to_thread(native_folder_dialog, session.workspace)
    if not chosen:
        await session.push(
            Outbound.NOTICE,
            level="info",
            text="没有可用的原生选择器，请直接填路径",
        )
        return
    target = str(args.get("target") or "agent").strip().lower()
    if target == "codex":
        await _codex_set_workspace(session, {"path": chosen})
        return
    if target == "claude":
        await _claude_set_workspace(session, {"path": chosen})
        return
    await _set_workspace(session, {"path": chosen})


async def _set_workspace(session: GuiSession, args: dict[str, Any]) -> None:
    """Point the agent at a different project.

    Background turns keep their own workspace/agent; only the view moves.
    """
    raw = str(args.get("path", "")).strip()
    if not raw:
        return
    try:
        session.set_workspace(Path(raw))
    except NotADirectoryError as error:
        await session.push(Outbound.ERROR, message=str(error))
        return

    from ..workspace.paths import invalidate_path_index

    invalidate_path_index()
    session.last_turn_refs = []
    # Agent workspace is independent of Codex / Claude panel sessions.
    # Panel cwd changes only via codex_set_workspace / claude_set_workspace.
    await session.push_workspace(session.live_workspaces())
    await session.push_transcript()
    await session.push_all()
    await session.push_skills()


async def _list_skills(session: GuiSession, args: dict[str, Any]) -> None:
    from .workspace import RecentWorkspaces

    await session.push_skills()
    await session.push_workspace(RecentWorkspaces.load().existing())


async def _reload_skills(session: GuiSession, args: dict[str, Any]) -> None:

    session.skills = SkillLibrary(session.workspace, session.config.skill_paths).load()
    session.agent.skills = session.skills
    session.agent.ctx.skills = session.skills
    session.agent.invalidate_system_prompt()
    await session.push_skills()
    await session.push_status()


async def _add_skill_path(session: GuiSession, args: dict[str, Any]) -> None:
    """Add a directory to the skill search path."""
    raw = str(args.get("path", "")).strip()
    if not raw:
        from .workspace import native_folder_dialog

        raw = await asyncio.to_thread(native_folder_dialog, session.workspace) or ""
    if not raw:
        return
    if not Path(raw).expanduser().is_dir():
        await session.push(Outbound.ERROR, message=f"{raw} 不是一个目录")
        return
    if raw in session.config.skill_paths:
        return
    session.config.skill_paths.append(raw)
    await _reload_skills(session, {})
    await _persist_config(session)


async def _remove_skill_path(session: GuiSession, args: dict[str, Any]) -> None:
    raw = str(args.get("path", ""))
    if raw in session.config.skill_paths:
        session.config.skill_paths.remove(raw)
        await _reload_skills(session, {})
        await _persist_config(session)


# --------------------------------------------------------------------------
# heartbeat
# --------------------------------------------------------------------------


def _cap(args: dict[str, Any], key: str) -> float:
    """Read one cap. Blank means the cap is off, not that it is zero."""
    raw = str(args.get(key, "")).strip()
    if not raw:
        return float(NO_LIMIT)
    value = float(raw)
    if value < 0:
        raise ValueError(f"{key} 不能是负数")
    return value


async def _start_heartbeat(session: GuiSession, args: dict[str, Any]) -> None:
    """Accept the caps and switch the composer into goal mode.

    The dialog deliberately has no goal field. A goal worth handing to an
    unattended loop is a paragraph, not a phrase, and a one-line input in a
    modal invites a one-line goal — which is exactly the kind of
    under-specified instruction that wastes a spend cap. So the caps are set
    here and the goal is typed in the composer, at full size.
    """
    try:
        limits = HeartbeatLimits(
            max_iterations=int(_cap(args, "iterations")),
            max_cost=_cap(args, "cost"),
            max_minutes=_cap(args, "minutes"),
        )
        limits.validate()
    except ValueError as error:
        await session.push(Outbound.ERROR, message=str(error))
        return
    if session.heartbeat.active:
        await session.push(Outbound.ERROR, message="心跳已经在跑了，先停掉")
        return

    session.armed_limits = limits
    session.armed_interval = float(args.get("interval", HEARTBEAT_DEFAULT_INTERVAL))
    await session.push(
        Outbound.HEARTBEAT, active=False, armed=True, limits=limits.describe(True)
    )
    await session.push_status()


async def _launch_heartbeat(session: GuiSession, goal: str) -> bool:
    """Turn an armed heartbeat plus a composer prompt into a running loop."""
    limits = session.armed_limits
    if limits is None:
        return False
    session.armed_limits = None
    try:
        state = session.heartbeat.start(goal, limits, session.armed_interval)
    except (RuntimeError, ValueError) as error:
        await session.push(Outbound.ERROR, message=str(error))
        await session.push_status()
        return False
    await session.push(
        Outbound.HEARTBEAT,
        active=True,
        armed=False,
        goal=state.goal,
        iterations=0,
        limits=limits.describe(True),
    )
    await session.push_status()
    return True


async def _stop_heartbeat(session: GuiSession, args: dict[str, Any]) -> None:
    session.armed_limits = None
    session.heartbeat.stop(StopReason.USER_STOPPED)
    await session.push(Outbound.HEARTBEAT, active=False, armed=False)
    await session.push_status()





async def _set_context(session: GuiSession, args: dict[str, Any]) -> None:
    """Resize the context window for this session.

    A model may declare several usable window sizes; a smaller one costs less
    per turn but compacts sooner, so it is the user's call rather than ours.
    """
    try:
        tokens = int(args.get("tokens", 0))
    except (TypeError, ValueError):
        return
    if tokens <= 0:
        return
    selection = session.agent.selection
    selection.context = tokens
    session.agent.set_selection(selection)
    await session.push_status()
    await session.push_context()


async def _set_auto_compact(session: GuiSession, args: dict[str, Any]) -> None:
    """Turn automatic compaction on or off.

    It is on by default and should stay that way: the manual buttons exist
    for forcing it early, not for doing the job by hand.
    """
    session.config.context.auto_compact = bool(args.get("enabled", True))
    await session.push_status()
    await _persist_config(session)


async def _set_auto_classify(session: GuiSession, args: dict[str, Any]) -> None:
    """Turn automatic plan-mode routing on or off."""
    session.config.planning.auto_classify = bool(args.get("enabled", True))
    await session.push_status()
    await _persist_config(session)


async def _set_auto_apply_edits(session: GuiSession, args: dict[str, Any]) -> None:
    """When on + yolo mode, Write/Edit skip the pending-review queue."""
    session.config.ui.auto_apply_edits = bool(args.get("enabled", False))
    await session.push_status()
    await _persist_config(session)
    if session.config.ui.auto_apply_edits and session.permissions.mode == "yolo":
        if session.edit_review.pending():
            session.edit_review.apply_all()
            await session.push_edit_review()
            await session.push(
                Outbound.NOTICE,
                level="info",
                text=session.msg("yolo.auto_apply_pending"),
            )


async def _set_language(session: GuiSession, args: dict[str, Any]) -> None:
    """Change the GUI language preference (stored in UI prefs, not config.yaml)."""
    from .locale import LANGUAGE_LABELS

    language = session.set_ui_language(str(args.get("language", "auto")))
    label = LANGUAGE_LABELS.get(language, language)
    await session.push_status()
    await session.push_config()
    await session.push(
        Outbound.NOTICE, level="info", text=session.msg("language.set", label=label)
    )


async def _set_plan_mode(session: GuiSession, args: dict[str, Any]) -> None:
    """Enter or leave plan mode from the UI badge / settings."""
    wanted = bool(args.get("enabled"))
    session.permissions.set_plan_mode(wanted)
    session.agent.invalidate_system_prompt()
    if not wanted:
        session.plan = None
    await session.push(
        Outbound.NOTICE,
        level="info",
        text=session.msg("plan.enter") if wanted else session.msg("plan.exit"),
    )
    await session.push_status()


async def _set_explore_mode(session: GuiSession, args: dict[str, Any]) -> None:
    """Enter or leave read-only explore mode."""
    wanted = bool(args.get("enabled"))
    session.permissions.set_explore_mode(wanted)
    session.agent.invalidate_system_prompt()
    if wanted:
        session.plan = None
    await session.push(
        Outbound.NOTICE,
        level="info",
        text=session.msg("explore.enter") if wanted else session.msg("explore.exit"),
    )
    await session.push_status()


async def _set_draft(session: GuiSession, args: dict[str, Any]) -> None:
    """Persist unsent composer text for the open session."""
    session.drafts.set(session.session.meta.id, str(args.get("text", "")))


async def _learn_skills(session: GuiSession, args: dict[str, Any]) -> None:
    """Mine recent sessions for skill candidates (GUI entry for /learn)."""
    from ..providers.router import Selection
    from ..workflows.learning import collect_digests, mine_skills

    await session.push(Outbound.NOTICE, level="info", text="正在扫描近期会话…")
    digests = collect_digests(session.sessions, workspace=session.workspace)
    binding = session.config.role("cheap") or session.config.role("main")
    if binding is None:
        await session.push(
            Outbound.LEARN_RESULT,
            ok=False,
            text="需要先配置 main 或 cheap 角色才能学习。",
            candidates=[],
        )
        return
    try:
        candidates = await mine_skills(
            digests, session.router, Selection.from_binding(binding)
        )
    except Exception as error:  # noqa: BLE001
        await session.push(
            Outbound.LEARN_RESULT,
            ok=False,
            text=f"学习失败：{error}",
            candidates=[],
        )
        return
    session._learning_candidates = {item.name: item for item in candidates}
    await session.push(
        Outbound.LEARN_RESULT,
        ok=True,
        text=(
            f"扫到 {len(candidates)} 个候选（不会自动写入）。"
            if candidates
            else "没有找到足够重复的习惯。"
        ),
        candidates=[
            {
                "name": item.name,
                "description": item.description,
                "occurrences": item.occurrences,
            }
            for item in candidates
        ],
    )


async def _market_quote(session: GuiSession, args: dict[str, Any]) -> None:
    """Fetch a single quote for the market panel (no trading)."""
    from ..market.alerts import check_alerts
    from ..market.router import MarketDataError

    symbol = str(args.get("symbol", "")).strip()
    if not symbol:
        await session.push(
            Outbound.MARKET_RESULT,
            ok=False,
            text=session.msg("backtest.need_symbol"),
            symbol="",
        )
        return
    if session.market is None:
        await session.push(
            Outbound.MARKET_RESULT,
            ok=False,
            text=session.msg("market.disabled"),
            symbol=symbol,
        )
        return
    try:
        quote = session.market.quote(symbol)
        await session.push(
            Outbound.MARKET_RESULT, ok=True, text=quote.describe(), symbol=quote.symbol
        )
        last = float(getattr(quote, "price", 0) or 0)
        if last:
            for alert, price in check_alerts(session.workspace, quote.symbol, last):
                await session.push(
                    Outbound.MARKET_ALERT,
                    text=session.msg(
                        "alert.fired",
                        symbol=alert.symbol,
                        price=price,
                        op=alert.op,
                        target=alert.price,
                    ),
                    symbol=alert.symbol,
                    alert=alert.public(),
                )
    except MarketDataError as error:
        await session.push(
            Outbound.MARKET_RESULT, ok=False, text=str(error), symbol=symbol
        )
    except Exception as error:  # noqa: BLE001
        await session.push(
            Outbound.MARKET_RESULT, ok=False, text=str(error), symbol=symbol
        )


async def _market_history(session: GuiSession, args: dict[str, Any]) -> None:
    """Show recent OHLCV bars as text (no real trading)."""
    from ..market.router import MarketDataError

    symbol = str(args.get("symbol", "")).strip()
    count = max(5, min(int(args.get("count") or 30), 120))
    if not symbol:
        await session.push(
            Outbound.MARKET_RESULT, ok=False, text="请输入股票代码", symbol=""
        )
        return
    if session.market is None:
        await session.push(
            Outbound.MARKET_RESULT,
            ok=False,
            text=session.msg("market.disabled"),
            symbol=symbol,
        )
        return
    try:
        series = session.market.history(symbol, count=count)
        bars = [
            {
                "day": bar.day.isoformat(),
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
            }
            for bar in series.bars
        ]
        await session.push(
            Outbound.MARKET_RESULT,
            ok=True,
            text=series.describe(),
            symbol=symbol,
            bars=bars,
            adjusted=bool(series.adjusted),
            source=series.source or "",
        )
    except MarketDataError as error:
        await session.push(
            Outbound.MARKET_RESULT, ok=False, text=str(error), symbol=symbol, bars=[]
        )
    except Exception as error:  # noqa: BLE001
        await session.push(
            Outbound.MARKET_RESULT, ok=False, text=str(error), symbol=symbol, bars=[]
        )


async def _paper_status(session: GuiSession, args: dict[str, Any]) -> None:
    """Show the paper trading book (never real money)."""
    if session.paper_book is None:
        await session.push(
            Outbound.MARKET_RESULT,
            ok=False,
            text=session.msg("market.disabled"),
            symbol="",
        )
        return
    await session.push(
        Outbound.MARKET_RESULT,
        ok=True,
        text=session.paper_book.describe() + "\n\n（纸上交易 · 无真实下单）",
        symbol="",
    )


async def _market_backtest(session: GuiSession, args: dict[str, Any]) -> None:
    """Run a simple MA-cross paper backtest and return an equity curve."""
    from ..market.backtest import ma_cross_backtest
    from ..market.router import MarketDataError

    symbol = str(args.get("symbol", "")).strip()
    fast = max(2, min(int(args.get("fast") or 5), 60))
    slow = max(fast + 1, min(int(args.get("slow") or 20), 120))
    count = max(40, min(int(args.get("count") or 120), 260))
    if not symbol:
        await session.push(
            Outbound.MARKET_RESULT,
            ok=False,
            text=session.msg("backtest.need_symbol"),
            symbol="",
        )
        return
    if session.market is None:
        await session.push(
            Outbound.MARKET_RESULT,
            ok=False,
            text=session.msg("market.disabled"),
            symbol=symbol,
        )
        return
    try:
        series = session.market.history(symbol, count=count)
        result = ma_cross_backtest(series, fast=fast, slow=slow)
        await session.push(
            Outbound.MARKET_RESULT,
            ok=True,
            text=result.describe(),
            symbol=symbol,
            equity=result.equity,
            backtest={
                "fast": result.fast,
                "slow": result.slow,
                "trades": result.trades,
                "return_pct": result.return_pct,
                "max_drawdown_pct": result.max_drawdown_pct,
            },
        )
    except MarketDataError as error:
        await session.push(
            Outbound.MARKET_RESULT, ok=False, text=str(error), symbol=symbol
        )
    except Exception as error:  # noqa: BLE001
        await session.push(
            Outbound.MARKET_RESULT, ok=False, text=str(error), symbol=symbol
        )


async def _market_alert_add(session: GuiSession, args: dict[str, Any]) -> None:
    from ..market.alerts import add_alert

    symbol = str(args.get("symbol", "")).strip()
    try:
        price = float(args.get("price"))
    except (TypeError, ValueError):
        await session.push(Outbound.ERROR, message=session.msg("backtest.need_symbol"))
        return
    if not symbol:
        await session.push(Outbound.ERROR, message=session.msg("backtest.need_symbol"))
        return
    alert = add_alert(
        session.workspace,
        symbol,
        price,
        op=str(args.get("op") or ">="),
        note=str(args.get("note") or ""),
    )
    await session.push(
        Outbound.NOTICE,
        level="info",
        text=session.msg(
            "alert.added", symbol=alert.symbol, op=alert.op, price=alert.price
        ),
    )
    await _market_alert_list(session, {})


async def _market_alert_list(session: GuiSession, args: dict[str, Any]) -> None:
    from ..market.alerts import load_alerts

    alerts = load_alerts(session.workspace)
    text = session.msg("alert.none") if not alerts else "\n".join(
        f"{a.symbol} {a.op} {a.price}" + (" · armed" if a.armed else " · fired")
        for a in alerts
    )
    await session.push(
        Outbound.MARKET_RESULT,
        ok=True,
        text=text,
        symbol="",
        alerts=[a.public() for a in alerts],
    )


async def _market_alert_delete(session: GuiSession, args: dict[str, Any]) -> None:
    from ..market.alerts import delete_alert

    if not delete_alert(session.workspace, str(args.get("id", ""))):
        await session.push(Outbound.ERROR, message=session.msg("alert.none"))
        return
    await _market_alert_list(session, {})


async def _open_url(session: GuiSession, args: dict[str, Any]) -> None:
    """Open an http(s) URL in the system browser (Sponsors, donate pages)."""
    import webbrowser
    from urllib.parse import urlparse

    raw = str(args.get("url") or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        await session.push(Outbound.NOTICE, level="warn", text="无效的链接")
        return
    try:
        await asyncio.to_thread(webbrowser.open, raw)
    except Exception as exc:  # noqa: BLE001
        await session.push(Outbound.NOTICE, level="error", text=f"无法打开链接: {exc}")


async def _open_path(session: GuiSession, args: dict[str, Any]) -> None:
    """Reveal or open a workspace/session path in the OS."""
    from .workspace import open_path_default, reveal_path

    raw = str(args.get("path", "")).strip()
    if not raw:
        return
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = session.workspace / path
    try:
        path = path.resolve()
    except OSError as error:
        await session.push(Outbound.NOTICE, level="warn", text=str(error))
        return
    allowed = (session.workspace.resolve(), session.session.directory.resolve())
    if not any(_is_under(path, root) for root in allowed):
        await session.push(
            Outbound.NOTICE, level="warn", text="只能打开当前工作区或本会话内的路径"
        )
        return
    if not path.exists():
        await session.push(Outbound.NOTICE, level="warn", text=f"路径不存在：{path}")
        return
    mode = str(args.get("mode") or "reveal")
    try:
        if mode == "open":
            await asyncio.to_thread(open_path_default, path)
        else:
            await asyncio.to_thread(reveal_path, path)
    except OSError as error:
        await session.push(Outbound.NOTICE, level="warn", text=str(error))


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


_CANVAS_SUFFIXES = (".md", ".markdown", ".html", ".htm", ".png", ".jpg", ".jpeg", ".webp", ".gif")


def _looks_canvas_path(path: str) -> bool:
    lowered = path.lower().replace("\\", "/")
    return lowered.endswith(_CANVAS_SUFFIXES)


# --------------------------------------------------------------------------
# the routing table
#
# Defined last so every handler above is already bound. Keeping it here
# also makes it obvious at a glance which commands exist.
# --------------------------------------------------------------------------


async def _forget_workspace(session: GuiSession, args: dict[str, Any]) -> None:
    """Remove a project from the sidebar, with everything under it.

    Deletes that project's sessions; the directory on disk is untouched. The
    frontend confirms first, because there is no undo for the transcripts.
    """
    from .workspace import RecentWorkspaces

    raw = str(args.get("path", "")).strip()
    if not raw:
        return
    target = Path(raw)

    removed = session.sessions.delete_all(workspace=target)
    session.forget_workspace(target)

    # Persist it, or the project reappears on the next start and the removal
    # looks like it silently failed.
    stored = RecentWorkspaces.load()
    stored.paths = [p for p in stored.paths if Path(p) != target]
    stored.save()

    # Removing the *open* project used to switch the workspace to a fallback,
    # which called remember_workspace and put the directory straight back —
    # so the last project could never be removed. Forgetting a project is a
    # change to a list, not to where the agent is working: the open session
    # keeps its directory, it simply stops being offered as a shortcut.
    if session.sessions.open(session.session.meta.id) is None:
        await _start_fresh_session(session)
        await session.push_workspace(session.live_workspaces())
        return

    await session.push(
        Outbound.NOTICE, level="info", text=f"已移除项目 {target.name}（{removed} 个会话）"
    )
    await session.push_sessions()
    # The chip row above the composer is fed by a different message than the
    # sidebar; without this the removed project stayed on screen there.
    await session.push_workspace(session.live_workspaces())



# --------------------------------------------------------------------------
# capabilities that reach outside the workspace
# --------------------------------------------------------------------------

#: Config sections the user can switch on from the settings panel.
CAPABILITIES = ("desktop", "browser")


async def _set_capability(session: GuiSession, args: dict[str, Any]) -> None:
    """Turn desktop or browser control on or off.

    The registry is rebuilt rather than filtered at call time, so a disabled
    capability is not merely refused — the tools are absent from the schema
    the model is given, and it cannot ask for what it cannot see.
    """
    name = str(args.get("id", "")).strip()
    if name not in CAPABILITIES:
        await session.push(Outbound.ERROR, message=f"没有这个能力：{name}")
        return
    wanted = bool(args.get("enabled"))

    section = getattr(session.config, name)
    if section.enabled == wanted:
        await session.push_config()
        return
    section.enabled = wanted

    session.rebuild_tools()
    await session.push(
        Outbound.NOTICE,
        level="warn" if wanted else "info",
        text=("已开启" if wanted else "已关闭") + f"「{name}」",
    )
    await session.push_config()
    await session.push_status()
    await _persist_config(session)


async def _set_account_proxy(session: GuiSession, args: dict[str, Any]) -> None:
    """Change one account's network route, without touching the others."""
    account = session.config.account(str(args.get("id", "")))
    if account is None:
        await session.push(Outbound.ERROR, message="没有这个账号")
        return
    try:
        account.proxy = proxy.normalise(str(args.get("proxy", "")))
    except proxy.ProxyError as error:
        await session.push(Outbound.ERROR, message=str(error))
        return

    # The pooled client was built with the old route baked in.
    await session.router.reset_client(account.id)
    await session.push(
        Outbound.NOTICE,
        level="info",
        text=f"{account.id} 的网络出口改为 {proxy.describe(account)}",
    )
    await session.push_config()
    await _persist_config(session)


async def _warn_if_interrupted_session(session: GuiSession) -> None:
    from ..session.store import unanswered_tool_calls

    pending = unanswered_tool_calls(session.agent.messages)
    if pending:
        await session.push(
            Outbound.NOTICE,
            level="warn",
            text=f"上次会话可能被打断（{len(pending)} 个工具结果未完成）；已自动修补，可直接继续。",
        )


async def _list_paths(session: GuiSession, args: dict[str, Any]) -> None:
    from ..workspace.paths import list_paths

    paths = list_paths(
        session.workspace,
        query=str(args.get("query", "")),
        kind=str(args.get("kind", "") or ""),
        ext=str(args.get("ext", "") or ""),
    )
    await session.push(
        Outbound.PATH_INDEX,
        paths=paths,
        query=str(args.get("query", "")),
        kind=str(args.get("kind", "") or ""),
        ext=str(args.get("ext", "") or ""),
    )


async def _search_content(session: GuiSession, args: dict[str, Any]) -> None:
    from ..workspace.search import search_content

    query = str(args.get("query", "")).strip()
    if not query:
        await session.push(Outbound.ERROR, message=session.msg("search.empty"))
        return
    hits = await asyncio.to_thread(
        search_content,
        session.workspace,
        query,
        glob=str(args.get("glob", "") or ""),
    )
    await session.push(
        Outbound.SEARCH_HITS,
        query=query,
        hits=hits,
        glob=str(args.get("glob", "") or ""),
    )


async def _list_tree(session: GuiSession, args: dict[str, Any]) -> None:
    from ..workspace.paths import list_tree

    nodes = list_tree(session.workspace, rel=str(args.get("path", "")))
    await session.push(
        Outbound.FILE_TREE, path=str(args.get("path", "")), nodes=nodes
    )


async def _preview_path(session: GuiSession, args: dict[str, Any]) -> None:
    from ..workspace.paths import read_path_preview

    preview = read_path_preview(session.workspace, str(args.get("path", "")))
    await session.push(Outbound.FILE_PREVIEW, **preview)


async def _list_rules(session: GuiSession, args: dict[str, Any]) -> None:
    from ..rules import list_rules

    await session.push(Outbound.RULES_LIST, rules=list_rules(session.workspace))


async def _save_rule(session: GuiSession, args: dict[str, Any]) -> None:
    from ..rules import write_rule

    try:
        path = write_rule(
            str(args.get("scope", "project")),
            str(args.get("name", "")),
            str(args.get("body", "")),
            session.workspace,
        )
    except ValueError as error:
        await session.push(Outbound.ERROR, message=str(error))
        return
    session.agent.invalidate_system_prompt()
    await session.push(Outbound.NOTICE, level="info", text=f"已保存规则 {path.name}")
    await _list_rules(session, {})
    await session.push_context()


async def _delete_rule(session: GuiSession, args: dict[str, Any]) -> None:
    from ..rules import delete_rule

    ok = delete_rule(
        str(args.get("scope", "project")),
        str(args.get("name", "")),
        session.workspace,
    )
    if not ok:
        await session.push(Outbound.ERROR, message="没有这条规则")
        return
    session.agent.invalidate_system_prompt()
    await session.push(Outbound.NOTICE, level="info", text="已删除规则")
    await _list_rules(session, {})
    await session.push_context()


async def _list_memories(session: GuiSession, args: dict[str, Any]) -> None:
    from ..memories import load_memories

    await session.push(
        Outbound.MEMORIES,
        memories=[m.public() for m in load_memories(session.workspace)],
    )


async def _add_memory(session: GuiSession, args: dict[str, Any]) -> None:
    from ..memories import add_memory

    text = str(args.get("text", "")).strip()
    if not text:
        await session.push(Outbound.ERROR, message="记忆内容不能为空")
        return
    add_memory(session.workspace, text, pinned=bool(args.get("pinned", True)))
    session.agent.invalidate_system_prompt()
    await _list_memories(session, {})
    await session.push_context()


async def _update_memory(session: GuiSession, args: dict[str, Any]) -> None:
    from ..memories import update_memory

    item = update_memory(
        session.workspace,
        str(args.get("id", "")),
        text=args.get("text"),
        pinned=args.get("pinned"),
    )
    if item is None:
        await session.push(Outbound.ERROR, message="没有这条记忆")
        return
    session.agent.invalidate_system_prompt()
    await _list_memories(session, {})
    await session.push_context()


async def _delete_memory(session: GuiSession, args: dict[str, Any]) -> None:
    from ..memories import delete_memory

    if not delete_memory(session.workspace, str(args.get("id", ""))):
        await session.push(Outbound.ERROR, message="没有这条记忆")
        return
    session.agent.invalidate_system_prompt()
    await _list_memories(session, {})
    await session.push_context()


async def _list_quest(session: GuiSession, args: dict[str, Any]) -> None:
    from ..quest import load_quest

    sid = session.session.meta.id
    quest = load_quest(session.workspace, session_id=sid)
    await session.push(
        Outbound.QUEST,
        quest=quest.public() if quest else None,
        session_id=sid,
    )


async def _start_quest(session: GuiSession, args: dict[str, Any]) -> None:
    from ..quest import start_quest

    goal = str(args.get("goal", "")).strip()
    steps = [str(s).strip() for s in (args.get("steps") or []) if str(s).strip()]
    if not goal:
        await session.push(Outbound.ERROR, message="Quest 目标不能为空")
        return
    sid = session.session.meta.id
    quest = start_quest(session.workspace, goal, steps, session_id=sid)
    await session.push(Outbound.QUEST, quest=quest.public(), session_id=sid)
    await session.push(
        Outbound.NOTICE, level="info", text=session.msg("quest.started", goal=goal)
    )


async def _quest_step(session: GuiSession, args: dict[str, Any]) -> None:
    from ..quest import set_step_status

    sid = session.session.meta.id
    quest = set_step_status(
        session.workspace,
        str(args.get("id", "")),
        str(args.get("status", "done")),  # type: ignore[arg-type]
        note=str(args.get("note", "")),
        blocked_reason=str(args.get("blocked_reason", "")),
        session_id=sid,
    )
    if quest is None:
        await session.push(Outbound.ERROR, message="没有这个 Quest 步骤")
        return
    await session.push(Outbound.QUEST, quest=quest.public(), session_id=sid)


async def _resume_quest(session: GuiSession, args: dict[str, Any]) -> None:
    from ..quest import quest_prompt_hint, resume_quest

    sid = session.session.meta.id
    quest = resume_quest(session.workspace, session_id=sid)
    if quest is None:
        await session.push(Outbound.ERROR, message=session.msg("quest.none"))
        return
    await session.push(Outbound.QUEST, quest=quest.public(), session_id=sid)
    auto = args.get("auto", True)
    if auto and not session.busy:
        step = quest.active_step_title() or quest.goal
        prompt = (
            f"{quest_prompt_hint(session.workspace, session_id=sid)}"
            f"Continue the active Quest from step: {step}. "
            f"Do not restart finished work. If the last failure was a Verify, "
            f"fix the root cause then re-verify."
        )
        await session.push(
            Outbound.NOTICE, level="info", text=session.msg("quest.resumed")
        )
        session._turn_task = asyncio.create_task(
            run_turn(session, prompt.strip(), display_text=f"[Quest resume] {step}")
        )
    else:
        await session.push(
            Outbound.NOTICE, level="info", text=session.msg("quest.resumed_idle")
        )


async def _continue_work(session: GuiSession, args: dict[str, Any]) -> None:
    """One-click resume for open Quest steps or unfinished TodoWrite items."""
    if session.busy:
        await session.push(
            Outbound.NOTICE, level="warn", text=session.msg("busy")
        )
        return
    sid = session.session.meta.id
    from ..quest import load_quest, quest_prompt_hint, resume_quest

    quest = load_quest(session.workspace, session_id=sid)
    if quest is not None and quest.status not in {"idle", "done"}:
        await _resume_quest(session, {"auto": True})
        return
    todos = list(
        session.session_todos.get(sid)
        or session.agent.ctx.todos
        or session.session.todos
        or []
    )
    open_items = open_todos_remaining(todos)
    if not open_items:
        await session.push(
            Outbound.NOTICE, level="info", text=session.msg("todo.nothing_open")
        )
        return
    # Prefer resume_quest's path when a quest exists but was idle — otherwise
    # seed from todos alone.
    if quest is not None and quest.status == "blocked":
        resume_quest(session.workspace, session_id=sid)
    prompt = _todo_continue_prompt(todos)
    await session.push(
        Outbound.NOTICE,
        level="info",
        text=session.msg("todo.continuing", n=len(open_items)),
    )
    session._turn_task = asyncio.create_task(
        run_turn(
            session,
            prompt,
            display_text=session.msg("todo.continue_label", n=len(open_items)),
        )
    )


async def _clear_quest(session: GuiSession, args: dict[str, Any]) -> None:
    from ..quest import save_quest

    sid = session.session.meta.id
    save_quest(session.workspace, None, session_id=sid)
    await session.push(Outbound.QUEST, quest=None, session_id=sid)


async def _capture_screen(session: GuiSession, args: dict[str, Any]) -> None:
    """Interactive region capture → composer attach → image editor."""
    from .capture import CaptureCancelledError, CaptureError, capture_screen

    hide_self = bool(args.get("hide_self", True))
    interactive = bool(args.get("interactive", True))
    try:
        shot = await capture_screen(hide_self=hide_self, interactive=interactive)
    except CaptureCancelledError:
        await session.push(Outbound.SCREENSHOT, cancelled=True)
        return
    except CaptureError as error:
        await session.push(Outbound.NOTICE, level="warn", text=str(error))
        return
    except Exception as error:  # noqa: BLE001 - surface to the UI
        await session.push(
            Outbound.NOTICE, level="warn", text=f"截屏失败：{error}"
        )
        return
    await session.push(Outbound.SCREENSHOT, **shot.to_wire())


async def _new_window(session: GuiSession, args: dict[str, Any]) -> None:
    """Spawn another local Dr.Wang process; optionally pick a different folder."""
    import subprocess
    import sys

    from .workspace import native_folder_dialog

    raw = str(args.get("path") or "").strip()
    if not raw:
        chosen = await asyncio.to_thread(native_folder_dialog, session.workspace)
        if not chosen:
            await session.push(Outbound.NOTICE, level="info", text="已取消打开新窗口")
            return
        raw = chosen
    target = Path(raw).expanduser().resolve()
    if not target.is_dir():
        await session.push(Outbound.ERROR, message=f"不是目录：{target}")
        return
    cmd = [
        sys.executable,
        "-m",
        "aiharness",
        "-C",
        str(target),
        "gui",
    ]
    try:
        subprocess.Popen(cmd, close_fds=True)  # noqa: S603 - fixed argv
    except OSError as error:
        await session.push(Outbound.ERROR, message=f"无法打开新窗口：{error}")
        return
    await session.push(
        Outbound.NOTICE,
        level="info",
        text=f"已打开本机新窗口 · {target.name}（独立进程与会话，不云端同步）",
    )


async def _save_canvas(session: GuiSession, args: dict[str, Any]) -> None:
    """Write an editable Canvas preview back to the workspace."""
    rel = str(args.get("path", "")).strip().replace("\\", "/")
    content = args.get("content")
    if not rel:
        await session.push(Outbound.ERROR, message="画布路径不能为空")
        return
    if content is None:
        await session.push(Outbound.ERROR, message="缺少要保存的内容")
        return
    root = session.workspace.resolve()
    path = (root / rel).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        await session.push(Outbound.ERROR, message="路径超出工作区")
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content), encoding="utf-8")
    except OSError as error:
        await session.push(Outbound.ERROR, message=f"保存失败：{error}")
        return
    from ..workspace.paths import invalidate_path_index

    invalidate_path_index(session.workspace)
    await session.push(Outbound.NOTICE, level="info", text=f"已保存画布 {rel}")
    await session.push(Outbound.CANVAS_HINT, path=rel, kind="write")


# --------------------------------------------------------------------------
# Codex panel (sibling runtime)
# --------------------------------------------------------------------------


async def _codex_set_home(session: GuiSession, args: dict[str, Any]) -> None:
    kind = str(
        args.get("selection")
        or args.get("profile_id")
        or args.get("home_kind")
        or args.get("kind")
        or "kimi"
    ).strip()
    await session.codex.set_selection(kind)


async def _codex_set_profile(session: GuiSession, args: dict[str, Any]) -> None:
    await _codex_set_home(session, args)


async def _codex_upsert_profile(session: GuiSession, args: dict[str, Any]) -> None:
    await session.codex.upsert_profile(args)


async def _codex_delete_profile(session: GuiSession, args: dict[str, Any]) -> None:
    await session.codex.delete_profile(str(args.get("id") or args.get("profile_id") or ""))


async def _codex_import_account(session: GuiSession, args: dict[str, Any]) -> None:
    account_id = str(args.get("account_id") or args.get("id") or "").strip()
    if not account_id:
        raise ValueError("account_id is required")
    await session.codex.import_account(
        account_id,
        activate=bool(args.get("activate", True)),
    )


async def _codex_start(session: GuiSession, args: dict[str, Any]) -> None:
    kind = str(
        args.get("selection") or args.get("profile_id") or args.get("home_kind") or ""
    ).strip()
    if kind:
        await session.codex.set_selection(kind)
    else:
        await session.codex.start()


async def _codex_stop(session: GuiSession, args: dict[str, Any]) -> None:
    await session.codex.stop()


async def _codex_prompt(session: GuiSession, args: dict[str, Any]) -> None:
    text = str(args.get("text", "")).strip()
    images = args.get("images") or []
    if not isinstance(images, list):
        images = []
    if not text and not images:
        return
    await session.codex.prompt(text, images=images)


async def _codex_interrupt(session: GuiSession, args: dict[str, Any]) -> None:
    await session.codex.interrupt()


async def _codex_approve(session: GuiSession, args: dict[str, Any]) -> None:
    session.resolve(str(args.get("id", "")), args.get("decision"))


async def _codex_set_model(session: GuiSession, args: dict[str, Any]) -> None:
    await session.codex.set_model(str(args.get("model") or args.get("id") or ""))


async def _codex_set_effort(session: GuiSession, args: dict[str, Any]) -> None:
    await session.codex.set_effort(str(args.get("effort") or args.get("value") or ""))


async def _codex_set_mode(session: GuiSession, args: dict[str, Any]) -> None:
    mode = str(args.get("mode") or "").strip().lower()
    if mode not in VALID_MODES:
        await session.push(Outbound.ERROR, message=f"unknown mode '{mode}'")
        return
    await session.codex.set_permission_mode(mode)


async def _codex_new_session(session: GuiSession, args: dict[str, Any]) -> None:
    raw = str(args.get("path") or args.get("workspace") or "").strip()
    workspace = Path(raw).expanduser().resolve() if raw else None
    if workspace is not None and not workspace.is_dir():
        await session.push(Outbound.ERROR, message=f"不是目录：{workspace}")
        return
    await session.codex.new_session(workspace)


async def _codex_open_session(session: GuiSession, args: dict[str, Any]) -> None:
    await session.codex.open_session(str(args.get("id") or args.get("session_id") or ""))


async def _codex_delete_session(session: GuiSession, args: dict[str, Any]) -> None:
    await session.codex.delete_session(str(args.get("id") or args.get("session_id") or ""))


async def _codex_archive_session(session: GuiSession, args: dict[str, Any]) -> None:
    await session.codex.archive_session(
        str(args.get("id") or args.get("session_id") or ""),
        bool(args.get("archived", True)),
    )


async def _codex_set_workspace(session: GuiSession, args: dict[str, Any]) -> None:
    raw = str(args.get("path") or args.get("workspace") or "").strip()
    if not raw:
        return
    path = Path(raw).expanduser().resolve()
    if not path.is_dir():
        await session.push(Outbound.ERROR, message=f"不是目录：{path}")
        return
    await session.codex.set_panel_workspace(path)


async def _codex_forget_workspace(session: GuiSession, args: dict[str, Any]) -> None:
    raw = str(args.get("path") or "").strip()
    if not raw:
        return
    await session.codex.forget_workspace(Path(raw))


async def _codex_toggle_archived(session: GuiSession, args: dict[str, Any]) -> None:
    show = bool(args.get("show", not session.codex.show_archived))
    await session.codex.set_show_archived(show)


async def _claude_set_profile(session: GuiSession, args: dict[str, Any]) -> None:
    selection = str(args.get("selection") or args.get("profile_id") or args.get("id") or "").strip()
    await session.claude.set_selection(selection)


async def _claude_set_model(session: GuiSession, args: dict[str, Any]) -> None:
    await session.claude.set_model(str(args.get("model") or args.get("id") or ""))


async def _claude_set_effort(session: GuiSession, args: dict[str, Any]) -> None:
    await session.claude.set_effort(str(args.get("effort") or args.get("value") or ""))


async def _claude_set_mode(session: GuiSession, args: dict[str, Any]) -> None:
    mode = str(args.get("mode") or "").strip().lower()
    if mode not in VALID_MODES:
        await session.push(Outbound.ERROR, message=f"unknown mode '{mode}'")
        return
    await session.claude.set_permission_mode(mode)


async def _claude_upsert_profile(session: GuiSession, args: dict[str, Any]) -> None:
    await session.claude.upsert_profile(args)


async def _claude_delete_profile(session: GuiSession, args: dict[str, Any]) -> None:
    await session.claude.delete_profile(str(args.get("id") or args.get("profile_id") or ""))


async def _claude_import_account(session: GuiSession, args: dict[str, Any]) -> None:
    account_id = str(args.get("account_id") or args.get("id") or "").strip()
    if not account_id:
        raise ValueError("account_id is required")
    await session.claude.import_account(
        account_id,
        activate=bool(args.get("activate", True)),
    )


async def _claude_start(session: GuiSession, args: dict[str, Any]) -> None:
    selection = str(args.get("selection") or args.get("profile_id") or "").strip()
    if selection:
        await session.claude.set_selection(selection)
    else:
        await session.claude.start()


async def _claude_stop(session: GuiSession, args: dict[str, Any]) -> None:
    await session.claude.stop()


async def _claude_prompt(session: GuiSession, args: dict[str, Any]) -> None:
    text = str(args.get("text", "")).strip()
    images = args.get("images") or []
    if not isinstance(images, list):
        images = []
    if not text and not images:
        return
    await session.claude.prompt(text, images=images)


async def _claude_interrupt(session: GuiSession, args: dict[str, Any]) -> None:
    await session.claude.interrupt()


async def _claude_approve(session: GuiSession, args: dict[str, Any]) -> None:
    session.resolve(str(args.get("id", "")), args.get("decision"))


async def _claude_login(session: GuiSession, args: dict[str, Any]) -> None:
    selection = str(args.get("selection") or args.get("profile_id") or "").strip()
    if selection:
        session.claude.selection = selection
        try:
            session.claude.profiles.set_active(selection)
        except ValueError:
            pass
    await session.claude.login()


async def _claude_new_session(session: GuiSession, args: dict[str, Any]) -> None:
    raw = str(args.get("path") or args.get("workspace") or "").strip()
    workspace = Path(raw).expanduser().resolve() if raw else None
    if workspace is not None and not workspace.is_dir():
        await session.push(Outbound.ERROR, message=f"不是目录：{workspace}")
        return
    await session.claude.new_session(workspace)


async def _claude_open_session(session: GuiSession, args: dict[str, Any]) -> None:
    await session.claude.open_session(str(args.get("id") or args.get("session_id") or ""))


async def _claude_delete_session(session: GuiSession, args: dict[str, Any]) -> None:
    await session.claude.delete_session(str(args.get("id") or args.get("session_id") or ""))


async def _claude_archive_session(session: GuiSession, args: dict[str, Any]) -> None:
    await session.claude.archive_session(
        str(args.get("id") or args.get("session_id") or ""),
        bool(args.get("archived", True)),
    )


async def _claude_set_workspace(session: GuiSession, args: dict[str, Any]) -> None:
    raw = str(args.get("path") or args.get("workspace") or "").strip()
    if not raw:
        return
    path = Path(raw).expanduser().resolve()
    if not path.is_dir():
        await session.push(Outbound.ERROR, message=f"不是目录：{path}")
        return
    await session.claude.set_panel_workspace(path)


async def _claude_forget_workspace(session: GuiSession, args: dict[str, Any]) -> None:
    raw = str(args.get("path") or "").strip()
    if not raw:
        return
    await session.claude.forget_workspace(Path(raw))


async def _claude_toggle_archived(session: GuiSession, args: dict[str, Any]) -> None:
    show = bool(args.get("show", not session.claude.show_archived))
    await session.claude.set_show_archived(show)


HANDLERS: dict[Inbound, Any] = {
    Inbound.PROMPT: _prompt,
    Inbound.STEER: _steer,
    Inbound.INTERRUPT: _interrupt,
    Inbound.ANSWER: _answer,
    Inbound.APPROVE: _approve,
    Inbound.PLAN_DECISION: _plan_decision,
    Inbound.NEW_SESSION: _new_session,
    Inbound.OPEN_SESSION: _open_session,
    Inbound.DELETE_SESSION: _delete_session,
    Inbound.ARCHIVE_SESSION: _archive_session,
    Inbound.TOGGLE_ARCHIVED: _toggle_archived,
    Inbound.SET_WORKSPACE: _set_workspace,
    Inbound.PICK_WORKSPACE: _pick_workspace,
    Inbound.FORGET_WORKSPACE: _forget_workspace,
    Inbound.ADD_SKILL_PATH: _add_skill_path,
    Inbound.REMOVE_SKILL_PATH: _remove_skill_path,
    Inbound.LIST_SKILLS: _list_skills,
    Inbound.RELOAD_SKILLS: _reload_skills,
    Inbound.CLEAR_SESSION: _clear_session,
    Inbound.REWIND_TURN: _rewind_turn,
    Inbound.COMPACT: _compact,
    Inbound.UNCOMPACT: _uncompact,
    Inbound.SET_MODEL: _set_model,
    Inbound.SET_MODE: _set_mode,
    Inbound.SET_EFFORT: _set_effort,
    Inbound.SET_THEME: lambda s, a: s.push_status(),
    Inbound.SET_LANGUAGE: _set_language,
    Inbound.ADD_ACCOUNT: _add_account,
    Inbound.REMOVE_ACCOUNT: _remove_account,
    Inbound.LIST_ACCOUNT_MODELS: _list_account_models,
    Inbound.ADD_MODEL: _add_model,
    Inbound.REMOVE_MODEL: _remove_model,
    Inbound.SET_ROLE: _set_role,
    Inbound.SET_CAPABILITY: _set_capability,
    Inbound.SET_ACCOUNT_PROXY: _set_account_proxy,
    Inbound.SAVE_CONFIG: _save_config,
    Inbound.REFRESH: _refresh,
    Inbound.START_HEARTBEAT: _start_heartbeat,
    Inbound.STOP_HEARTBEAT: _stop_heartbeat,
    Inbound.EDIT_DECISION: _edit_decision,
    Inbound.LIST_PATHS: _list_paths,
    Inbound.LIST_TREE: _list_tree,
    Inbound.PREVIEW_PATH: _preview_path,
    Inbound.LIST_RULES: _list_rules,
    Inbound.SAVE_RULE: _save_rule,
    Inbound.DELETE_RULE: _delete_rule,
    Inbound.LIST_MEMORIES: _list_memories,
    Inbound.ADD_MEMORY: _add_memory,
    Inbound.UPDATE_MEMORY: _update_memory,
    Inbound.DELETE_MEMORY: _delete_memory,
    Inbound.LIST_QUEST: _list_quest,
    Inbound.START_QUEST: _start_quest,
    Inbound.QUEST_STEP: _quest_step,
    Inbound.RESUME_QUEST: _resume_quest,
    Inbound.CONTINUE_WORK: _continue_work,
    Inbound.CLEAR_QUEST: _clear_quest,
    Inbound.NEW_WINDOW: _new_window,
    Inbound.CAPTURE_SCREEN: _capture_screen,
    Inbound.SAVE_CANVAS: _save_canvas,
    Inbound.SET_CONTEXT: _set_context,
    Inbound.SET_AUTO_COMPACT: _set_auto_compact,
    Inbound.SET_AUTO_CLASSIFY: _set_auto_classify,
    Inbound.SET_AUTO_APPLY_EDITS: _set_auto_apply_edits,
    Inbound.SET_PLAN_MODE: _set_plan_mode,
    Inbound.SET_EXPLORE_MODE: _set_explore_mode,
    Inbound.SET_DRAFT: _set_draft,
    Inbound.OPEN_PATH: _open_path,
    Inbound.OPEN_URL: _open_url,
    Inbound.SET_MODEL_VISION: _set_model_vision,
    Inbound.LEARN_SKILLS: _learn_skills,
    Inbound.MARKET_QUOTE: _market_quote,
    Inbound.MARKET_HISTORY: _market_history,
    Inbound.MARKET_BACKTEST: _market_backtest,
    Inbound.MARKET_ALERT_ADD: _market_alert_add,
    Inbound.MARKET_ALERT_LIST: _market_alert_list,
    Inbound.MARKET_ALERT_DELETE: _market_alert_delete,
    Inbound.PAPER_STATUS: _paper_status,
    Inbound.SEARCH_CONTENT: _search_content,
    Inbound.CODEX_SET_HOME: _codex_set_home,
    Inbound.CODEX_SET_PROFILE: _codex_set_profile,
    Inbound.CODEX_UPSERT_PROFILE: _codex_upsert_profile,
    Inbound.CODEX_DELETE_PROFILE: _codex_delete_profile,
    Inbound.CODEX_IMPORT_ACCOUNT: _codex_import_account,
    Inbound.CODEX_START: _codex_start,
    Inbound.CODEX_STOP: _codex_stop,
    Inbound.CODEX_PROMPT: _codex_prompt,
    Inbound.CODEX_INTERRUPT: _codex_interrupt,
    Inbound.CODEX_APPROVE: _codex_approve,
    Inbound.CODEX_SET_MODEL: _codex_set_model,
    Inbound.CODEX_SET_EFFORT: _codex_set_effort,
    Inbound.CODEX_SET_MODE: _codex_set_mode,
    Inbound.CODEX_NEW_SESSION: _codex_new_session,
    Inbound.CODEX_OPEN_SESSION: _codex_open_session,
    Inbound.CODEX_DELETE_SESSION: _codex_delete_session,
    Inbound.CODEX_ARCHIVE_SESSION: _codex_archive_session,
    Inbound.CODEX_SET_WORKSPACE: _codex_set_workspace,
    Inbound.CODEX_FORGET_WORKSPACE: _codex_forget_workspace,
    Inbound.CODEX_TOGGLE_ARCHIVED: _codex_toggle_archived,
    Inbound.CLAUDE_SET_PROFILE: _claude_set_profile,
    Inbound.CLAUDE_UPSERT_PROFILE: _claude_upsert_profile,
    Inbound.CLAUDE_DELETE_PROFILE: _claude_delete_profile,
    Inbound.CLAUDE_IMPORT_ACCOUNT: _claude_import_account,
    Inbound.CLAUDE_START: _claude_start,
    Inbound.CLAUDE_STOP: _claude_stop,
    Inbound.CLAUDE_PROMPT: _claude_prompt,
    Inbound.CLAUDE_INTERRUPT: _claude_interrupt,
    Inbound.CLAUDE_APPROVE: _claude_approve,
    Inbound.CLAUDE_SET_MODEL: _claude_set_model,
    Inbound.CLAUDE_SET_EFFORT: _claude_set_effort,
    Inbound.CLAUDE_SET_MODE: _claude_set_mode,
    Inbound.CLAUDE_LOGIN: _claude_login,
    Inbound.CLAUDE_NEW_SESSION: _claude_new_session,
    Inbound.CLAUDE_OPEN_SESSION: _claude_open_session,
    Inbound.CLAUDE_DELETE_SESSION: _claude_delete_session,
    Inbound.CLAUDE_ARCHIVE_SESSION: _claude_archive_session,
    Inbound.CLAUDE_SET_WORKSPACE: _claude_set_workspace,
    Inbound.CLAUDE_FORGET_WORKSPACE: _claude_forget_workspace,
    Inbound.CLAUDE_TOGGLE_ARCHIVED: _claude_toggle_archived,
}
