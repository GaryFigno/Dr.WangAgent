"""The agent loop: model turn -> tool calls -> results -> repeat."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config.schema import Config
from ..constants import (
    BASH_OUTER_TIMEOUT,
    DOOM_LOOP_THRESHOLD,
    INTERRUPTED_TOOL_RESULT,
    LITE_EXCLUDED_TOOLS,
    MALFORMED_ARGS_PREVIEW_CHARS,
    OVERFLOW_COMPACT_RETRIES,
    PARALLEL_TOOL_TIMEOUT,
    REMINDER_EVERY_TURNS,
    TOOL_INVOKE_TIMEOUT,
    UI_CONTEXT_BAR_WIDTH,
    UI_STEERING_PREVIEW_CHARS,
)
from ..permissions import PermissionEngine
from ..providers.base import (
    Message,
    ProviderError,
    ReasoningDelta,
    StreamDone,
    TextDelta,
    ToolCall,
    ToolCallDelta,
    Usage,
)
from ..providers.router import NoRouteError, Router, Selection, compute_cost
from ..session.attachments import (
    AttachmentRef,
    ImageAttachment,
    attachment_labels,
    expand_message_for_wire,
    model_supports_vision,
    save_attachments,
)
from ..session.store import (
    CompactionRecord,
    SessionHandle,
    pair_tool_calls,
    unanswered_tool_calls,
)
from ..skills import SkillLibrary
from ..tools.base import ToolContext, ToolRegistry, ToolResult
from .context import (
    ContextBreakdown,
    ContextMeter,
    compact,
    estimate_messages,
    measure_context,
    microcompact_reads,
    prepare_tool_result_for_model,
    prune_old_tool_outputs,
    truncate_tool_result,
)
from .prompts import build_environment_note, build_system_prompt

#: Tools that only observe, and so may run concurrently with each other.
PARALLEL_SAFE = frozenset({"Read", "Glob", "Grep", "Skill", "ListSkills"})


def _tool_fingerprint(call: ToolCall) -> str:
    return f"{call.name}\0{call.arguments}"


def _looks_like_context_overflow(error: Exception) -> bool:
    text = str(error).lower()
    needles = (
        "context length",
        "context_length",
        "maximum context",
        "context window",
        "too many tokens",
        "token limit",
        "prompt is too long",
        "prompt too long",
        "exceeds the model",
        "max_tokens",
        "request too large",
    )
    return any(needle in text for needle in needles)


def _tail_accepts_tool_results(
    messages: list[Message], pending: list[ToolCall]
) -> bool:
    """True when filler tool results can be appended without scrambling order."""
    if not messages or not pending:
        return False
    index = len(messages) - 1
    while index >= 0 and messages[index].role == "tool":
        index -= 1
    if index < 0 or messages[index].role != "assistant":
        return False
    owner_ids = {call.id for call in messages[index].tool_calls}
    return all(call.id in owner_ids for call in pending)


# --------------------------------------------------------------------------
# events emitted to the UI
# --------------------------------------------------------------------------


@dataclass
class Thinking:
    """A chunk of the model's reasoning stream."""

    text: str


@dataclass
class Text:
    """A chunk of the model's visible answer."""

    text: str


@dataclass
class ToolStart:
    call_id: str
    name: str
    args: dict[str, Any]


@dataclass
class ToolEnd:
    call_id: str
    name: str
    result: ToolResult
    duration: float


@dataclass
class Notice:
    text: str
    level: str = "info"  # info | warn | error


@dataclass
class TurnEnd:
    usage: Usage
    cost: float
    model: str
    account: str
    turns: int


@dataclass
class Compacted:
    """Emitted when the context was compacted, with everything needed to show it.

    The UI draws a divider at this point in the transcript. Compaction is the
    only operation that changes what the agent knows without the user asking,
    so it is reported as a first-class event rather than a log line.
    """

    summary: str
    tokens_before: int
    tokens_after: int
    replaced: int
    model: str
    automatic: bool


@dataclass
class Done:
    text: str
    interrupted: bool = False


AgentEvent = (
    Thinking | Text | ToolStart | ToolEnd | Notice | TurnEnd | Compacted | Done
)


@dataclass
class CacheStats:
    """Prompt-cache effectiveness for this agent process (resets on restart)."""

    prompt_tokens: int = 0
    cached_tokens: int = 0

    @property
    def hit_rate(self) -> float:
        if self.prompt_tokens <= 0:
            return 0.0
        return self.cached_tokens / self.prompt_tokens


@dataclass
class AgentState:
    turns: int = 0
    total_usage: Usage = field(default_factory=Usage)
    total_cost: float = 0.0
    cache: CacheStats = field(default_factory=CacheStats)


# --------------------------------------------------------------------------
# the agent
# --------------------------------------------------------------------------


class Agent:
    """Owns one conversation.

    The UI drives it by iterating :meth:`run`, which yields events as the
    model streams and tools execute. When a :class:`SessionHandle` is
    supplied every message is written through to disk before it is used, so
    an interrupted process loses nothing.
    """

    def __init__(
        self,
        config: Config,
        router: Router,
        tools: ToolRegistry,
        permissions: PermissionEngine,
        workspace: Path,
        *,
        skills: SkillLibrary | None = None,
        selection: Selection | None = None,
        system_prompt: str | None = None,
        tool_context: ToolContext | None = None,
        session: SessionHandle | None = None,
        subagent: bool = False,
    ):
        self.config = config
        self.router = router
        self.tools = tools
        self.permissions = permissions
        self.workspace = workspace
        self.skills = skills
        self.session = session
        self.subagent = subagent

        self.selection = selection or Selection.for_session(config, session)

        self.ctx = tool_context or ToolContext(
            workspace=workspace,
            config=config,
            permissions=permissions,
            router=router,
            skills=skills,
        )
        self.state = AgentState()
        self._messages: list[Message] = list(session.view()) if session else []
        self._system_prompt = system_prompt
        self._rules_section = ""
        self._rules_sources: list[str] = []
        self._memory_sources: list[str] = []
        #: ``full`` exposes every registered tool; ``lite`` drops multi-agent
        #: orchestration tools for small classified requests (T30).
        self.tool_profile: str = "full"
        self._last_assistant: Message | None = None
        self._cancel = asyncio.Event()
        self._meter = ContextMeter(window=self._window(), cfg=config.context)
        #: Mid-turn user guidance queued while a run is in flight; drained
        #: between model/tool rounds so the current turn can be steered.
        self._steering: list[str] = []
        self._doom_fp: str | None = None
        self._doom_count: int = 0
        self._overflow_retries_left: int = OVERFLOW_COMPACT_RETRIES
        self._post_compact_reminder: bool = False
        self._last_model_overflow: bool = False
        #: Mid-stream prefetch of PARALLEL_SAFE tools, keyed by fingerprint.
        self._prefetch: dict[str, asyncio.Task[ToolResult]] = {}
        #: Wire-only reminder appended for the next model call (not persisted).
        self._ephemeral_reminder: str | None = None

    # -- configuration ----------------------------------------------------

    def _window(self) -> int:
        model = self.config.model(self.selection.model_id)
        if model is None:
            return self.config.context.fallback_window
        return model.context_for(self.selection.context)

    def system_prompt(self) -> str:
        """Return the cache-stable system prompt, building it once."""
        if self._system_prompt is None:
            from ..memories import memories_section
            from ..rules import load_rules
            from ..tools.shell import find_shell

            _, _, dialect = find_shell()
            rules_section, self._rules_sources = load_rules(self.workspace)
            mem_section, mem_sources = memories_section(self.workspace)
            self._memory_sources = mem_sources
            if mem_section:
                rules_section = (
                    f"{rules_section}\n\n{mem_section}" if rules_section else mem_section
                )
            self._rules_section = rules_section
            self._system_prompt = build_system_prompt(
                self.workspace,
                shell=dialect,
                skills_section=self.skills.prompt_section() if self.skills else "",
                rules_section=self._rules_section,
                extra=self.config.system_prompt_append,
                permission_mode=self.permissions.mode,
                plan_mode=self.permissions.plan_mode,
                explore_mode=self.permissions.explore_mode,
            )
        return self._system_prompt

    def set_selection(self, selection: Selection) -> None:
        """Switch model/account mid-session, keeping the transcript."""
        self.selection = selection
        self._meter = ContextMeter(window=self._window(), cfg=self.config.context)
        if self.session:
            self.session.set_model(selection.model_id, selection.account_id or "")

    def invalidate_system_prompt(self) -> None:
        """Force a rebuild, e.g. after the permission mode changed."""
        self._system_prompt = None

    # -- transcript -------------------------------------------------------

    @property
    def messages(self) -> list[Message]:
        return self._messages

    def _tool_specs(self) -> list[dict[str, Any]]:
        model = self.config.model(self.selection.model_id)
        if model is not None and not model.supports_tools:
            return []
        exclude = set(LITE_EXCLUDED_TOOLS) if self.tool_profile == "lite" else None
        return self.tools.specs(subagent=self.subagent, exclude=exclude)

    def set_tool_profile(self, profile: str) -> None:
        """Switch between ``full`` and ``lite`` tool sets for the next turns."""
        self.tool_profile = profile if profile in {"full", "lite"} else "full"

    def _wire_messages(self) -> list[Message]:
        """System prompt plus a provider-safe view of the conversation."""
        model = self.config.model(self.selection.model_id)
        vision = model_supports_vision(model)
        session_dir = self.session.directory if self.session else None
        history = [
            expand_message_for_wire(message, session_dir, include_images=vision)
            for message in pair_tool_calls(self._messages)
        ]
        wired = [Message(role="system", content=self.system_prompt())] + history
        if self._ephemeral_reminder:
            wired.append(
                Message(
                    role="user",
                    content=self._ephemeral_reminder,
                    meta={"reminder": True, "synthetic": True},
                )
            )
        return wired

    def _build_session_reminder(self) -> str | None:
        """Short status nudge for long tool loops (OpenCode-style reminders)."""
        todos = list(self.ctx.todos or [])
        open_todos = [
            item
            for item in todos
            if str(item.get("status", "pending")) not in {"completed", "cancelled"}
        ]
        if not open_todos and not self.permissions.explore_mode and not self.permissions.plan_mode:
            return None
        lines = ["<session_reminder>"]
        if self.permissions.explore_mode:
            lines.append("- Explore mode is on: read-only; do not attempt writes.")
        elif self.permissions.plan_mode:
            lines.append("- Plan mode is on: investigate, then PresentPlan.")
        if open_todos:
            lines.append(f"- Open todos ({len(open_todos)}):")
            for item in open_todos[:8]:
                content = str(item.get("content") or item.get("subject") or "").strip()
                status = str(item.get("status") or "pending")
                if content:
                    lines.append(f"  - [{status}] {content}")
            lines.append("- Update TodoWrite when progress changes; avoid re-reading unchanged files.")
        lines.append("</session_reminder>")
        return "\n".join(lines)

    def seal_unanswered_tool_calls(
        self, reason: str = INTERRUPTED_TOOL_RESULT
    ) -> int:
        """Persist synthetic tool results for still-open tool calls.

        Only appends when the unanswered calls sit at the tail of the
        transcript (the normal interrupt case). If a user message has already
        been written after the orphaned assistant turn, the durable record is
        left alone and :func:`pair_tool_calls` repairs the wire view instead
        — appending here would put tool results *after* the user turn and
        confuse the provider twice.

        Returns:
          How many filler tool messages were appended.
        """
        pending = unanswered_tool_calls(self._messages)
        if not pending or not _tail_accepts_tool_results(self._messages, pending):
            return 0
        for call in pending:
            self._record(call, ToolResult.error(reason))
        return len(pending)

    def add_user_message(
        self,
        text: str,
        images: list[ImageAttachment] | None = None,
    ) -> str | None:
        """Append a user turn, stamping it with the volatile environment note.

        The note is captured once, at creation, so replaying this message on
        later turns produces identical bytes and the prompt cache still hits.

        Args:
          text: What the user typed (may be empty when only images are sent).
          images: Ordered pasted/dragged images for this turn.

        Returns:
          A notice string when images were saved but the model cannot see them,
          otherwise ``None``.
        """
        note = build_environment_note(self.workspace, query=text or "")
        display = (text or "").strip()
        refs: list[AttachmentRef] = []
        degrade_notice: str | None = None
        if images:
            if self.session is None:
                raise RuntimeError("images require a persisted session")
            refs = save_attachments(self.session.directory, images)
            model = self.config.model(self.selection.model_id)
            if refs and not model_supports_vision(model):
                degrade_notice = (
                    "当前模型不支持图片：像素未发给模型，仅会话里保留原图。"
                    "可在设置里把该模型的「看图」改为「强制开」，或换成支持视觉的模型。"
                )
                labels = attachment_labels(refs)
                display = f"{display}\n\n{labels}".strip() if display else labels
        body = display or ("（见附图）" if refs else "")
        content = f"{note}\n\n{body}" if body else note
        meta: dict[str, Any] = {"user_text": display}
        if refs:
            meta["attachments"] = [ref.to_meta() for ref in refs]
        self._append(Message(role="user", content=content, meta=meta))
        self._reset_doom_loop()
        return degrade_notice

    def _append(self, message: Message) -> None:
        self._messages.append(message)
        if self.session:
            self.session.append(message)

    def clear(self) -> None:
        """Erase the conversation, in memory and on disk."""
        self._messages.clear()
        self.state = AgentState()
        self.ctx.read_files.clear()
        self.ctx.todos.clear()
        if self.session:
            self.session.clear_messages()

    def restore_full_history(self) -> int:
        """Undo compaction, putting the complete transcript back in context.

        Returns:
          How many compaction markers were dropped.
        """
        if not self.session:
            return 0
        dropped = self.session.drop_compactions()
        self._messages = list(self.session.view())
        return dropped

    def interrupt(self) -> None:
        self._cancel.set()
        self.ctx.cancel.set()
        self._cancel_prefetch()

    def _reset_doom_loop(self) -> None:
        self._doom_fp = None
        self._doom_count = 0

    def _cancel_prefetch(self) -> None:
        for task in self._prefetch.values():
            if not task.done():
                task.cancel()
        self._prefetch.clear()

    def _maybe_start_prefetch(self, slot: dict[str, str]) -> None:
        """Start a read-only tool as soon as its streamed args parse as JSON."""
        name = slot.get("name") or ""
        arguments = slot.get("arguments") or ""
        if name not in PARALLEL_SAFE or not arguments:
            return
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return
        if not isinstance(parsed, dict):
            return
        call = ToolCall(
            id=slot.get("id") or f"prefetch_{name}",
            name=name,
            arguments=arguments,
        )
        fingerprint = _tool_fingerprint(call)
        if fingerprint in self._prefetch:
            return
        self._prefetch[fingerprint] = asyncio.create_task(self._invoke_guarded(call))

    def steer(self, text: str) -> bool:
        """Queue mid-turn guidance for the next model gap.

        Returns:
          True when the text was accepted.
        """
        cleaned = (text or "").strip()
        if not cleaned:
            return False
        self._steering.append(cleaned)
        self._reset_doom_loop()
        return True

    def _take_steering(self) -> list[str]:
        items = self._steering
        self._steering = []
        return items

    def _inject_steering_messages(self, items: list[str]) -> list[Notice]:
        """Persist steering as user turns; return notices for the UI."""
        notices: list[Notice] = []
        for text in items:
            preview = text if len(text) <= UI_STEERING_PREVIEW_CHARS else text[:117] + "…"
            wired = f"<user_guidance>\n{text}\n</user_guidance>"
            notice = self.add_user_message(wired)
            notices.append(Notice(f"已注入中途引导：{preview}", level="info"))
            if notice:
                notices.append(Notice(notice, level="warn"))
        return notices

    # -- context reporting ------------------------------------------------

    def context_used(self) -> int:
        return self._meter.used(self._wire_messages(), self._tool_specs())

    def context_window(self) -> int:
        return self._meter.window

    def context_fraction(self) -> float:
        return self._meter.fraction(self._wire_messages(), self._tool_specs())

    def context_breakdown(self) -> ContextBreakdown:
        """Attribute the context window to what filled it.

        Calibrated the same way the meter is, so the total here matches the
        number in the status bar rather than quietly disagreeing with it.
        """
        # Ensure rules were loaded with the prompt.
        self.system_prompt()
        raw = measure_context(
            window=self._meter.window,
            system_prompt=self.system_prompt(),
            skills_section=self.skills.prompt_section() if self.skills else "",
            rules_section=self._rules_section,
            messages=self._messages,
            tool_specs=self._tool_specs(),
        )
        correction = self._meter.correction
        for item in raw.slices:
            item.tokens = int(item.tokens * correction)
            item.detail = {k: int(v * correction) for k, v in item.detail.items()}
        return raw

    def context_bar(self, width: int = UI_CONTEXT_BAR_WIDTH) -> str:
        filled = int(self.context_fraction() * width)
        return "█" * min(filled, width) + "░" * max(width - filled, 0)

    # -- the loop ---------------------------------------------------------

    async def run(
        self,
        user_input: str | None = None,
        images: list[ImageAttachment] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Drive the model until it stops calling tools.

        Args:
          user_input: Text to append as a user turn before running. Pass
            ``None`` to continue from the existing transcript.
          images: Optional ordered images attached to this user turn.

        Yields:
          :class:`AgentEvent` values describing streaming output, tool
          activity, notices and completion.
        """
        # Heal first, while unanswered tool_calls are still at the tail.
        # Adding the user message before sealing would push filler results
        # past the new user turn and leave the durable record scrambled.
        self.seal_unanswered_tool_calls()
        if user_input is not None or images:
            notice = self.add_user_message(user_input or "", images=images)
            if notice:
                yield Notice(notice, level="warn")
        self._cancel.clear()
        self.ctx.cancel.clear()
        self._overflow_retries_left = OVERFLOW_COMPACT_RETRIES
        self._last_model_overflow = False
        self._cancel_prefetch()

        try:
            async for event in self._drive_turns():
                yield event
                if isinstance(event, Done):
                    return
        finally:
            self._cancel_prefetch()
            # A hard asyncio cancellation can exit between appending an
            # assistant tool_calls message and recording its results.
            self.seal_unanswered_tool_calls()

    async def _drive_turns(self) -> AsyncIterator[AgentEvent]:
        """Run model/tool turns until the agent stops or is interrupted."""
        final_text = ""
        turns = 0
        while turns < self.config.max_agent_turns:
            if self._cancel.is_set():
                self.seal_unanswered_tool_calls()
                yield Done(final_text, interrupted=True)
                return

            compacted_reads = microcompact_reads(self._messages)
            if compacted_reads:
                yield Notice(
                    f"microcompacted {compacted_reads} older Read result(s)",
                    level="info",
                )

            if self.config.context.prune_tool_outputs:
                pruned = prune_old_tool_outputs(self._messages)
                if pruned:
                    yield Notice(
                        f"pruned {pruned} older tool output(s) to free context",
                        level="info",
                    )

            async for event in self._maybe_compact():
                yield event

            turns += 1
            self.state.turns += 1
            self._last_assistant = None
            self._last_model_overflow = False
            self._ephemeral_reminder = None
            if getattr(self, "_post_compact_reminder", False):
                self._post_compact_reminder = False
                self._ephemeral_reminder = (
                    "<system-reminder>Context was compacted. You still have "
                    "access to all tools in your system prompt. Continue from "
                    "the handoff note and the recent messages.</system-reminder>"
                )
                yield Notice("post-compaction tool reminder injected", level="info")
            elif turns > 1 and turns % REMINDER_EVERY_TURNS == 0:
                self._ephemeral_reminder = self._build_session_reminder()
                if self._ephemeral_reminder:
                    yield Notice("session reminder injected for this model call", level="info")

            async for event in self._model_turn(turns):
                yield event
            self._ephemeral_reminder = None

            assistant = self._last_assistant
            if assistant is None:
                if (
                    self._last_model_overflow
                    and self._overflow_retries_left > 0
                    and not self._cancel.is_set()
                ):
                    self._overflow_retries_left -= 1
                    yield Notice(
                        "context overflow — compacting and retrying once…",
                        level="warn",
                    )
                    async for event in self._compact_now(automatic=True):
                        yield event
                    continue
                self.seal_unanswered_tool_calls()
                yield Done(final_text, interrupted=self._cancel.is_set())
                return

            self._append(assistant)
            if assistant.content:
                final_text = assistant.content

            if self._cancel.is_set():
                self.seal_unanswered_tool_calls()
                yield Done(final_text, interrupted=True)
                return

            if not assistant.tool_calls:
                # If the user steered while we were answering, keep going
                # instead of ending the turn — that is "直接引导".
                steered = self._take_steering()
                if steered:
                    self.seal_unanswered_tool_calls()
                    for event in self._inject_steering_messages(steered):
                        yield event
                    continue
                yield Done(final_text)
                return

            async for event in self._run_tools(assistant.tool_calls):
                yield event

            steered = self._take_steering()
            if steered:
                self.seal_unanswered_tool_calls()
                for event in self._inject_steering_messages(steered):
                    yield event

        self.seal_unanswered_tool_calls()
        yield Notice(
            f"stopped after {turns} turns (max_agent_turns). "
            "Ask me to continue if the work is unfinished.",
            level="warn",
        )
        yield Done(final_text)

    async def _model_turn(self, turn_number: int) -> AsyncIterator[AgentEvent]:
        """Stream one model call, recording the assistant message on self."""
        messages = self._wire_messages()
        tool_specs = self._tool_specs()
        estimated = estimate_messages(messages)
        # Accumulate streamed tool slots so PARALLEL_SAFE calls can prefetch.
        tool_slots: dict[int, dict[str, str]] = {}
        self._cancel_prefetch()

        request = self.router.build_request(
            self.selection, messages, tools=tool_specs, stream=self.config.ui.stream
        )

        try:
            async for event in self.router.stream(self.selection, request, role="main"):
                if self._cancel.is_set():
                    break
                if isinstance(event, ReasoningDelta):
                    yield Thinking(event.text)
                elif isinstance(event, TextDelta):
                    yield Text(event.text)
                elif isinstance(event, ToolCallDelta):
                    slot = tool_slots.setdefault(
                        event.index, {"id": "", "name": "", "arguments": ""}
                    )
                    if event.id:
                        slot["id"] = event.id
                    if event.name:
                        slot["name"] += event.name
                    if event.arguments:
                        slot["arguments"] += event.arguments
                    self._maybe_start_prefetch(slot)
                elif isinstance(event, StreamDone):
                    # Drop prefetch entries that are not in the final tool list.
                    final_fps = {
                        _tool_fingerprint(call) for call in event.message.tool_calls
                    }
                    for fingerprint, task in list(self._prefetch.items()):
                        if fingerprint not in final_fps:
                            if not task.done():
                                task.cancel()
                            self._prefetch.pop(fingerprint, None)
                    yield self._finish_turn(event, estimated, turn_number)
        except NoRouteError as error:
            self._cancel_prefetch()
            if _looks_like_context_overflow(error):
                self._last_model_overflow = True
            yield Notice(str(error), level="error")
        except ProviderError as error:
            self._cancel_prefetch()
            if _looks_like_context_overflow(error):
                self._last_model_overflow = True
            yield Notice(f"model call failed: {error}", level="error")
        except asyncio.CancelledError:
            self._cancel.set()
            self._cancel_prefetch()
            raise

    def _finish_turn(self, event: StreamDone, estimated: int, turn_number: int) -> TurnEnd:
        """Record usage and cost for a completed model call."""
        self._last_assistant = event.message
        self.state.total_usage = self.state.total_usage + event.usage
        self.state.cache.prompt_tokens += event.usage.input_tokens
        self.state.cache.cached_tokens += event.usage.cached_tokens

        model = self.config.model(self.selection.model_id)
        cost = compute_cost(model, event.usage) if model else 0.0
        self.state.total_cost += cost
        if self.session:
            self.session.add_cost(cost)
            self.session.add_cache(
                event.usage.input_tokens, event.usage.cached_tokens
            )
        self._meter.calibrate(estimated, event.usage.input_tokens)

        return TurnEnd(
            usage=event.usage,
            cost=cost,
            model=event.model,
            account=event.account,
            turns=turn_number,
        )

    # -- tool execution ---------------------------------------------------

    async def _await_call_result(self, call: ToolCall) -> ToolResult:
        """Use a mid-stream prefetch when present; otherwise invoke now."""
        fingerprint = _tool_fingerprint(call)
        task = self._prefetch.pop(fingerprint, None)
        if task is not None:
            try:
                outcome = await task
            except asyncio.CancelledError:
                self._cancel.set()
                raise
            except Exception as error:  # noqa: BLE001
                return ToolResult.error(
                    f"{call.name} raised {type(error).__name__}: {error}"
                )
            return self._normalise(call, outcome)
        return await self._invoke_guarded(call)

    async def _run_tools(self, calls: list[ToolCall]) -> AsyncIterator[AgentEvent]:
        """Execute one assistant turn's tool calls.

        Read-only calls issued together run concurrently; anything that can
        mutate state runs in order, so permission prompts stay comprehensible
        and two writes never race. PARALLEL_SAFE calls may already be running
        from mid-stream prefetch.
        """
        parallel = [call for call in calls if call.name in PARALLEL_SAFE]
        serial = [call for call in calls if call.name not in PARALLEL_SAFE]

        if len(parallel) > 1:
            async for event in self._run_parallel_tools(parallel):
                yield event
        else:
            serial = parallel + serial

        for call in serial:
            if self._cancel.is_set():
                result = ToolResult.error(INTERRUPTED_TOOL_RESULT)
                self._record(call, result)
                yield ToolEnd(call.id, call.name, result, 0.0)
                continue
            yield ToolStart(call.id, call.name, self._safe_args(call))
            started = time.time()
            result = await self._await_call_result(call)
            self._record(call, result)
            yield ToolEnd(call.id, call.name, result, time.time() - started)
        self._cancel_prefetch()

    async def _run_parallel_tools(self, calls: list[ToolCall]) -> AsyncIterator[AgentEvent]:
        started: dict[str, float] = {}
        for call in calls:
            started[call.id] = time.time()
            yield ToolStart(call.id, call.name, self._safe_args(call))

        try:
            outcomes = await asyncio.gather(
                *(self._await_call_result(call) for call in calls),
                return_exceptions=True,
            )
        except asyncio.CancelledError:
            for call in calls:
                if call.id in {m.tool_call_id for m in self._messages if m.role == "tool"}:
                    continue
                result = ToolResult.error(INTERRUPTED_TOOL_RESULT)
                self._record(call, result)
                yield ToolEnd(call.id, call.name, result, time.time() - started[call.id])
            raise
        for call, outcome in zip(calls, outcomes, strict=True):
            result = self._normalise(call, outcome)
            self._record(call, result)
            yield ToolEnd(call.id, call.name, result, time.time() - started[call.id])

    def _invoke_timeout(self, call: ToolCall) -> float:
        """Per-call ceiling so one hung tool cannot stall the turn."""
        if call.name in PARALLEL_SAFE:
            return PARALLEL_TOOL_TIMEOUT
        if call.name == "Bash":
            return BASH_OUTER_TIMEOUT
        return TOOL_INVOKE_TIMEOUT

    async def _invoke_guarded(self, call: ToolCall) -> ToolResult:
        """Run one tool call, converting any failure into an error result."""
        timeout = self._invoke_timeout(call)
        try:
            return await asyncio.wait_for(self._invoke(call), timeout=timeout)
        except TimeoutError:
            return ToolResult.error(
                f"{call.name} timed out after {timeout:.0f}s and was abandoned"
            )
        except asyncio.CancelledError:
            # Must re-raise: swallowing leaves the turn "正在打断" while work
            # keeps going. Seal/filler in run() / run_turn covers the hole.
            self._cancel.set()
            self.ctx.cancel.set()
            raise
        except Exception as error:  # noqa: BLE001 - a tool must not kill the loop
            return ToolResult.error(f"{call.name} raised {type(error).__name__}: {error}")

    async def _invoke(self, call: ToolCall) -> ToolResult:
        tool = self.tools.get(call.name)
        if tool is None:
            available = ", ".join(self.tools.names())
            return ToolResult.error(f"No tool named '{call.name}'. Available: {available}")
        try:
            args = call.parsed()
        except ValueError as error:
            return ToolResult.error(f"{error}. Re-issue the call with valid JSON arguments.")

        fingerprint = _tool_fingerprint(call)
        if fingerprint == self._doom_fp:
            self._doom_count += 1
        else:
            self._doom_fp = fingerprint
            self._doom_count = 1
        if self._doom_count > DOOM_LOOP_THRESHOLD:
            return ToolResult.error(
                f"doom-loop: {call.name} repeated {self._doom_count} times with the "
                "same arguments. Stop retrying; change the approach or ask the user."
            )

        self.ctx.current_call_id = call.id
        try:
            return await tool.guarded_run(args, self.ctx)
        finally:
            self.ctx.current_call_id = ""

    def _normalise(self, call: ToolCall, outcome: Any) -> ToolResult:
        if isinstance(outcome, ToolResult):
            return outcome
        if isinstance(outcome, asyncio.CancelledError):
            return ToolResult.error(INTERRUPTED_TOOL_RESULT)
        if isinstance(outcome, BaseException):
            return ToolResult.error(f"{call.name} raised {type(outcome).__name__}: {outcome}")
        return ToolResult.error(f"{call.name} returned an unexpected value")

    def _record(self, call: ToolCall, result: ToolResult) -> None:
        """Persist a tool result: full text in meta, digest on the wire.

        The GUI still receives the original :class:`ToolResult` via ToolEnd.
        Main only sees ``content`` (and later prune digests).
        """
        full = truncate_tool_result(
            result.content, self.config.context.max_tool_result_chars
        )
        args = self._safe_args(call)
        command = str(args.get("command") or "") if call.name == "Bash" else ""
        wire = prepare_tool_result_for_model(
            call.name,
            full,
            is_error=result.is_error,
            command=command,
            context=self.config.context,
        )
        meta: dict[str, Any] = {"is_error": result.is_error, "tool": call.name}
        if wire != full:
            meta["full"] = full
        if command:
            meta["command"] = command[:240]
        if call.name == "Read":
            path = str(args.get("file_path") or "").strip()
            if path:
                meta["path"] = path[:480]
            if result.display.get("cached"):
                meta["cached_read"] = True
        self._append(
            Message(
                role="tool",
                content=wire,
                tool_call_id=call.id,
                name=call.name,
                meta=meta,
            )
        )

    def _safe_args(self, call: ToolCall) -> dict[str, Any]:
        try:
            return call.parsed()
        except ValueError:
            return {"_raw": call.arguments[:MALFORMED_ARGS_PREVIEW_CHARS]}

    # -- compaction -------------------------------------------------------

    async def _maybe_compact(self) -> AsyncIterator[AgentEvent]:
        """Compact the context if it is close to full."""
        if not self._meter.should_compact(self._wire_messages(), self._tool_specs()):
            return
        async for event in self._compact_now(automatic=True):
            yield event

    async def _compact_now(self, *, automatic: bool) -> AsyncIterator[AgentEvent]:
        binding = self.config.role("compactor")
        if binding is None:
            return

        before = self.context_used()
        if automatic:
            yield Notice(
                f"context {before:,}/{self._meter.window:,} tokens — compacting…",
                level="info",
            )

        try:
            new_messages, summary = await compact(
                self._wire_messages(),
                self.router,
                Selection.from_binding(binding),
                self.config.context,
                self._meter.window,
            )
        except (ProviderError, NoRouteError) as error:
            yield Notice(f"compaction failed ({error}); continuing uncompacted", level="warn")
            return

        if not summary:
            return

        retained = self._count_retained(new_messages)
        replaced_through = max(len(self._messages) - retained, 0)
        self._messages = [m for m in new_messages if m.role != "system"]
        after = self.context_used()

        if self.session:
            self.session.record_compaction(
                CompactionRecord(
                    at=time.time(),
                    replaced_through=replaced_through,
                    summary=summary,
                    model=binding.model,
                    tokens_before=before,
                    tokens_after=after,
                )
            )
        # OpenCode: after compaction the model may "forget" tools — nudge once.
        self._post_compact_reminder = True
        yield Compacted(
            summary=summary,
            tokens_before=before,
            tokens_after=after,
            replaced=replaced_through,
            model=binding.model,
            automatic=automatic,
        )

    def _count_retained(self, new_messages: list[Message]) -> int:
        """How many original messages survived a compaction verbatim."""
        return sum(
            1
            for message in new_messages
            if message.role != "system" and not message.meta.get("compacted")
        )

    async def compact_now(self) -> Compacted | None:
        """Compact on demand.

        Returns:
          The :class:`Compacted` event, or ``None`` when there was nothing
          worth compacting.
        """
        result: Compacted | None = None
        async for event in self._compact_now(automatic=False):
            if isinstance(event, Compacted):
                result = event
        return result
