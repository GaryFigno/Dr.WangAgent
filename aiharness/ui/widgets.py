"""Transcript widgets.

Streaming text is buffered and repainted on a timer rather than on every
delta. A fast model emits hundreds of chunks a second, and repainting a
Markdown widget that often is what makes terminal agents feel heavy.
"""

from __future__ import annotations

import time
from typing import Any

from rich.markdown import Markdown
from rich.text import Text
from textual.containers import Vertical
from textual.reactive import reactive
from textual.widgets import Static

from ..constants import (
    BREAKDOWN_EMPHASIS_SHARE,
    CACHE_HIT_WARN_THRESHOLD,
    MILLION,
    THOUSAND,
    UI_CONTEXT_BAR_WIDTH,
    UI_REFRESH_HZ,
    UI_TOOL_PREVIEW_CHARS,
)
from .mascot import Mascot
from .theme import CONTEXT_LEVEL_COLOURS, context_colour

#: Seconds between repaints while streaming.
REPAINT_INTERVAL = 1.0 / UI_REFRESH_HZ
#: Args rendered inline on a tool line, in preference order.
TOOL_ARG_KEYS = ("command", "file_path", "pattern", "path", "name", "task", "goal", "question")
#: Characters of an inline tool argument shown on the summary line.
TOOL_ARG_CHARS = 90
#: Separator between status-line fields.
SEPARATOR = "  ·  "
#: Characters of a compaction summary shown before it is expanded.
COMPACTION_PREVIEW_CHARS = 300


def _tool_headline(name: str, args: dict[str, Any]) -> str:
    """Render a tool call as one compact line."""
    for key in TOOL_ARG_KEYS:
        value = args.get(key)
        if value:
            text = " ".join(str(value).split())
            if len(text) > TOOL_ARG_CHARS:
                text = text[: TOOL_ARG_CHARS - 1] + "…"
            return f"{name}({text})"
    return f"{name}()"


class UserMessage(Static):
    """One turn typed by the user."""

    def __init__(self, text: str):
        super().__init__(Text(text, no_wrap=False), classes="entry user-message")


class NoticeLine(Static):
    """A harness message: compaction, routing failure, mode change."""

    GLYPHS = {"info": "·", "warn": "!", "error": "x"}

    def __init__(self, text: str, level: str = "info"):
        glyph = self.GLYPHS.get(level, "·")
        super().__init__(f"{glyph} {text}", classes=f"entry notice {level}")


class ReasoningBlock(Static):
    """The model's thinking stream, dimmed and collapsible."""

    def __init__(self) -> None:
        super().__init__("", classes="entry reasoning")
        self._buffer: list[str] = []
        self._last_paint = 0.0
        self.display = False

    def append(self, text: str) -> None:
        self._buffer.append(text)
        self.display = True
        now = time.monotonic()
        if now - self._last_paint >= REPAINT_INTERVAL:
            self._last_paint = now
            self._paint()

    def finish(self) -> None:
        self._paint()

    def _paint(self) -> None:
        body = "".join(self._buffer).strip()
        if not body:
            self.display = False
            return
        self.update(Text(body, no_wrap=False))


class AssistantMessage(Static):
    """The model's visible answer.

    Rendered as plain text while streaming and re-rendered as Markdown once
    the turn completes — Markdown parsing on every delta is the single most
    expensive thing a chat TUI can do.
    """

    def __init__(self) -> None:
        super().__init__("", classes="entry assistant-message")
        self._buffer: list[str] = []
        self._last_paint = 0.0
        self._finished = False

    @property
    def text(self) -> str:
        return "".join(self._buffer)

    def append(self, text: str) -> None:
        if self._finished:
            return
        self._buffer.append(text)
        now = time.monotonic()
        if now - self._last_paint >= REPAINT_INTERVAL:
            self._last_paint = now
            self.update(Text(self.text, no_wrap=False))

    def finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        body = self.text.strip()
        if not body:
            self.display = False
            return
        try:
            self.update(Markdown(body))
        except Exception:  # noqa: BLE001 - malformed markdown must not crash the UI
            self.update(Text(body, no_wrap=False))


class ToolCallEntry(Vertical):
    """A single tool invocation: one summary line, expandable detail."""

    expanded = reactive(False)

    def __init__(self, call_id: str, name: str, args: dict[str, Any]):
        super().__init__(classes="entry tool-call running")
        self.call_id = call_id
        self.tool_name = name
        self._args = args
        self._headline = Static(f"⟳ {_tool_headline(name, args)}")
        self._detail = Static("", classes="tool-detail")
        self._full_detail = ""

    def compose(self):
        yield self._headline
        yield self._detail

    def finish(self, result: Any, duration: float) -> None:
        """Fill in the outcome once the tool returns."""
        self.remove_class("running")
        self.add_class("failed" if result.is_error else "done")

        summary = result.summary or (result.content.splitlines() or [""])[0]
        glyph = "✗" if result.is_error else "✓"
        headline = f"{glyph} {_tool_headline(self.tool_name, self._args)}"
        if summary:
            headline += f"  — {' '.join(summary.split())[:120]}"
        headline += f"  ({duration:.1f}s)"
        self._headline.update(headline)

        self._full_detail = result.content
        preview = result.content[:UI_TOOL_PREVIEW_CHARS]
        if len(result.content) > UI_TOOL_PREVIEW_CHARS:
            preview += f"\n… ({len(result.content) - UI_TOOL_PREVIEW_CHARS} more characters)"
        self._detail.update(Text(preview, no_wrap=False))

    def toggle(self) -> None:
        self.expanded = not self.expanded

    def watch_expanded(self, expanded: bool) -> None:
        self._detail.set_class(expanded, "expanded")
        if expanded and self._full_detail:
            self._detail.update(Text(self._full_detail, no_wrap=False))

    def on_click(self) -> None:
        self.toggle()


class CompactionDivider(Vertical):
    """Marks the exact point in the transcript where context was compacted.

    Compaction is the one thing an agent does that silently changes what it
    knows. Leaving it invisible means the user cannot tell the difference
    between the model forgetting and the model never having been told. This
    divider sits in the scrollback at the boundary, shows what it cost, and
    expands to reveal the handoff note that replaced the messages above it.
    """

    expanded = reactive(False)

    def __init__(
        self,
        summary: str,
        *,
        tokens_before: int,
        tokens_after: int,
        replaced: int,
        model: str = "",
        chinese: bool = True,
    ):
        super().__init__(classes="entry compaction")
        self.summary = summary
        self.tokens_before = tokens_before
        self.tokens_after = tokens_after
        self.replaced = replaced
        self.model = model
        self.chinese = chinese
        self._headline = Static("", classes="compaction-headline")
        self._detail = Static("", classes="compaction-detail")

    def compose(self):
        yield self._headline
        yield self._detail

    def on_mount(self) -> None:
        self._headline.update(self._render_headline())
        self._detail.update(self._render_detail())

    def _render_headline(self) -> Text:
        saved = max(self.tokens_before - self.tokens_after, 0)
        if self.chinese:
            body = (
                f" 上下文已压缩 · {self.replaced} 条消息 → 摘要 · "
                f"{self.tokens_before:,} → {self.tokens_after:,} tokens（省 {saved:,}）"
            )
            hint = " 点击展开摘要 · /uncompact 还原全文 "
        else:
            body = (
                f" context compacted · {self.replaced} messages → summary · "
                f"{self.tokens_before:,} → {self.tokens_after:,} tokens (saved {saved:,})"
            )
            hint = " click to read the note · /uncompact restores everything "
        line = Text()
        line.append("━━", style="dim")
        line.append(body, style=f"bold {CONTEXT_LEVEL_COLOURS[1]}")
        line.append("━━", style="dim")
        line.append("\n")
        line.append(hint, style="dim italic")
        return line

    def _render_detail(self) -> Text:
        if self.expanded:
            header = "完整交接笔记：\n\n" if self.chinese else "Full handoff note:\n\n"
            return Text(header + self.summary, no_wrap=False)
        preview = " ".join(self.summary.split())[:COMPACTION_PREVIEW_CHARS]
        if len(self.summary) > COMPACTION_PREVIEW_CHARS:
            preview += "…"
        return Text(preview, style="dim", no_wrap=False)

    def watch_expanded(self, expanded: bool) -> None:
        self._detail.set_class(expanded, "expanded")
        self._detail.update(self._render_detail())

    def on_click(self) -> None:
        self.expanded = not self.expanded


#: Colour per context category, so the bar and the table agree.
SLICE_COLOURS = {
    "Messages": "#4f8ef7",
    "System tools": "#5aa9e6",
    "MCP tools": "#8fbcbb",
    "System prompt": "#e8a05c",
    "Skills": "#b48ead",
    "Memory files": "#a3be8c",
}
#: Colour for the unused remainder.
FREE_COLOUR = "#4a4a4a"
#: Width of the segmented context bar.
BREAKDOWN_BAR_WIDTH = 44


def render_context_breakdown(breakdown: Any, *, chinese: bool = False) -> Text:
    """Render a context breakdown as a segmented bar plus a table.

    Args:
      breakdown: A :class:`~aiharness.agent.context.ContextBreakdown`.
      chinese: Use Chinese labels.

    Returns:
      Styled text ready for a widget or the transcript.
    """
    title = "上下文窗口" if chinese else "Context window"
    out = Text()
    out.append(f"{title}  ", style="bold")
    out.append(
        f"{_compact_tokens(breakdown.used)} / {_compact_tokens(breakdown.window)} "
        f"({breakdown.fraction * 100:.0f}%)\n",
        style=context_colour(breakdown.fraction),
    )

    out.append_text(_segmented_bar(breakdown))
    out.append("\n\n")

    free_label = "剩余" if chinese else "Free space"
    for name, tokens, share in breakdown.rows():
        colour = FREE_COLOUR if name == "Free space" else SLICE_COLOURS.get(name, "#8a8a8a")
        label = free_label if name == "Free space" else name
        out.append("■ ", style=colour)
        out.append(f"{label:<16}", style="dim" if name == "Free space" else "")
        out.append(f"{_compact_tokens(tokens):>9}  ", style="dim")
        emphasis = "bold" if share > BREAKDOWN_EMPHASIS_SHARE else ""
        out.append(f"{share * 100:>5.1f}%\n", style=emphasis)

    detail = _slice_detail(breakdown)
    if detail:
        out.append("\n")
        out.append_text(detail)
    return out


def _segmented_bar(breakdown: Any) -> Text:
    """One bar, coloured by category, in the same order as the table."""
    bar = Text()
    drawn = 0
    for name, _tokens, share in breakdown.rows()[:-1]:  # free space is the remainder
        cells = int(round(share * BREAKDOWN_BAR_WIDTH))
        if cells <= 0:
            continue
        cells = min(cells, BREAKDOWN_BAR_WIDTH - drawn)
        bar.append("█" * cells, style=SLICE_COLOURS.get(name, "#8a8a8a"))
        drawn += cells
    bar.append("█" * max(BREAKDOWN_BAR_WIDTH - drawn, 0), style=FREE_COLOUR)
    return bar


def _slice_detail(breakdown: Any) -> Text | None:
    """Per-server MCP costs, which is where surprise bloat usually hides."""
    out = Text()
    for item in breakdown.slices:
        if not item.detail:
            continue
        out.append(f"{item.name} by server\n", style="dim bold")
        for key, tokens in sorted(item.detail.items(), key=lambda kv: -kv[1]):
            out.append(f"   {key:<14}{_compact_tokens(tokens):>9}\n", style="dim")
    return out if out.plain else None


def _compact_tokens(value: int) -> str:
    """Render a token count the way the eye reads it: 673.4k, not 673,412."""
    if value >= MILLION:
        return f"{value / MILLION:.1f}M"
    if value >= THOUSAND:
        return f"{value / THOUSAND:.1f}k"
    return str(value)


class ContextPanel(Static):
    """The context breakdown, shown on demand above the input."""

    def __init__(self) -> None:
        super().__init__("", id="context-panel")

    def render_breakdown(self, breakdown: Any, *, chinese: bool = False) -> None:
        self.update(render_context_breakdown(breakdown, chinese=chinese))
        self.add_class("visible")

    def hide(self) -> None:
        self.remove_class("visible")


class PlanPanel(Static):
    """The plan currently under discussion, pinned above the input.

    Kept visible for the whole of plan mode so the thing being argued about
    stays on screen while the argument happens.
    """

    def __init__(self) -> None:
        super().__init__("", id="plan-panel")

    def render_plan(self, plan: Any, *, chinese: bool = True) -> None:
        if plan is None:
            self.remove_class("visible")
            return
        header = "计划" if chinese else "Plan"
        state = ("已批准" if plan.approved else "待确认") if chinese else (
            "approved" if plan.approved else "awaiting approval"
        )
        body = Text()
        body.append(f"{header} · rev {plan.revision} · {state}\n", style="bold")
        body.append(f"{plan.goal}\n\n", style="")
        for index, step in enumerate(plan.steps, 1):
            body.append(f" {index}. {step.title}\n")
            if step.files:
                body.append(f"    {', '.join(step.files)}\n", style="dim")
        if plan.out_of_scope:
            label = "不做：" if chinese else "out of scope: "
            body.append(f"\n{label}{'; '.join(plan.out_of_scope)}\n", style="dim italic")
        self.update(body)
        self.add_class("visible")


class PetPanel(Static):
    """The mascot, parked in the sidebar as a live status indicator."""

    def __init__(self, mascot: Mascot) -> None:
        super().__init__("", id="pet")
        self.mascot = mascot

    def refresh_pet(self) -> None:
        drawing = self.mascot.render()
        if not drawing:
            self.display = False
            return
        self.display = True
        self.update(Text(drawing, style=self.mascot.rich_style() or ""))


class TodoPanel(Static):
    """The agent's current task list, pinned above the input."""

    GLYPHS = {"completed": "✓", "in_progress": "▸", "pending": "○"}

    def __init__(self) -> None:
        super().__init__("", id="todo-panel")

    def render_todos(self, todos: list[dict[str, Any]]) -> None:
        if not todos:
            self.remove_class("visible")
            return
        lines = []
        for todo in todos:
            status = todo.get("status", "pending")
            glyph = self.GLYPHS.get(status, "○")
            label = todo.get("activeForm" if status == "in_progress" else "content", "")
            style = "dim" if status == "completed" else ""
            lines.append(Text(f" {glyph} {label}", style=style))
        body = Text("\n").join(lines)
        done = sum(1 for t in todos if t.get("status") == "completed")
        header = Text(f"Tasks — {done}/{len(todos)}\n", style="bold")
        self.update(header + body)
        self.add_class("visible")


class StatusBar(Static):
    """Model, account, context usage, cache hit rate and spend."""

    def __init__(self) -> None:
        super().__init__("", id="status")

    def render_status(
        self,
        *,
        model: str,
        account: str,
        effort: str,
        mode: str,
        used: int,
        window: int,
        cache_hit: float,
        cost: float,
        jobs: int = 0,
        busy: bool = False,
    ) -> None:
        """Repaint the status line.

        Args:
          model: Active model id.
          account: Active API account id, or empty when the router picks.
          effort: Active effort level.
          mode: Permission mode.
          used: Estimated prompt tokens in the current context.
          window: The model's context window.
          cache_hit: Fraction of prompt tokens served from cache.
          cost: Session spend in USD.
          jobs: Number of scheduled jobs currently due or running.
          busy: Whether the agent is mid-turn.
        """
        target = f"{model}@{account}" if account else model
        line = Text()
        line.append(("⣾ " if busy else "") + target, style="bold")
        line.append(SEPARATOR)
        line.append(effort)
        line.append(SEPARATOR)
        line.append(mode, style="bold" if mode == "yolo" else "")
        line.append(SEPARATOR)
        line.append_text(context_gauge(used, window))
        line.append(SEPARATOR)
        line.append_text(_cache_text(cache_hit))
        line.append(SEPARATOR)
        line.append(f"${cost:.4f}")
        if jobs:
            line.append(SEPARATOR)
            line.append(f"{jobs} job(s)")
        self.update(line)


def _cache_text(hit_rate: float) -> Text:
    """Render the cache hit rate, warning when the prefix is churning."""
    label = f"cache {hit_rate * 100:.0f}%"
    if hit_rate < CACHE_HIT_WARN_THRESHOLD:
        return Text(label, style=f"bold {CONTEXT_LEVEL_COLOURS[2]}")
    return Text(label)


def context_gauge(used: int, window: int, width: int = UI_CONTEXT_BAR_WIDTH) -> Text:
    """Render the context-capacity bar.

    The bar changes colour as the window fills, so approaching a compaction is
    visible before it happens rather than being announced after the fact.

    Args:
      used: Estimated prompt tokens currently in context.
      window: The active model's context window.
      width: Bar width in cells.

    Returns:
      A styled :class:`~rich.text.Text` ready for the status line.
    """
    fraction = (used / window) if window else 0.0
    filled = min(int(fraction * width), width)
    colour = context_colour(fraction)

    bar = Text()
    bar.append("█" * filled, style=colour)
    bar.append("░" * (width - filled), style="dim")
    bar.append(f" {used:,}/{window:,}")
    bar.append(f" ({fraction * 100:.0f}%)", style=colour)
    return bar
