"""Token accounting and context compaction.

Compaction is performed *by us*, not by the model: no backend shrinks a
conversation on its own. When the estimated prompt approaches the window
limit, the harness calls a cheap model (the ``compactor`` role) to turn the
older half of the transcript into a dense handoff note, and sends that note
in place of those messages.

Nothing is discarded by this. The full transcript stays in the session log on
disk; compaction only changes what is transmitted. See
:mod:`aiharness.session.store` for the record/view split that makes this safe.

There is no tokenizer correct for every backend, so the estimate here is a
script-aware character heuristic that corrects itself against the
``prompt_tokens`` each response reports. The correction converges after a
couple of turns, which is enough to trigger compaction at the right moment.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..config.schema import ContextConfig
from ..constants import (
    BASH_ERROR_RESULT_CHARS,
    BASH_SUCCESS_RESULT_CHARS,
    CALIBRATION_RATIO_MAX,
    CALIBRATION_RATIO_MIN,
    CALIBRATION_SAMPLE_WINDOW,
    CHARS_PER_TOKEN_LATIN,
    COMPACT_MESSAGE_CHARS,
    COMPACT_MIN_MESSAGES,
    COMPACT_SUMMARY_MIN_TOKENS,
    COMPACT_TOOL_ARGS_CHARS,
    COMPACT_TOOL_RESULT_CHARS,
    PRUNE_DIGEST_CHARS,
    PRUNE_KEEP_USER_TURNS,
    PRUNE_MINIMUM_TOKENS,
    PRUNE_PROTECT_TOKENS,
    PRUNE_PROTECTED_TOOLS,
    READ_RESULT_CHARS,
    TOKENS_PER_CHAR_CJK,
    TOKENS_PER_MESSAGE_OVERHEAD,
    TOKENS_PER_TOOL_CALL_OVERHEAD,
    TRUNCATE_HEAD_FRACTION,
    TRUNCATE_TAIL_FRACTION,
)
from ..providers.base import Message

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..providers.router import Router, Selection

#: Characters that tokenize at roughly one token each.
CJK_RANGES = (
    "　-〿"  # CJK punctuation
    "぀-ヿ"  # kana
    "㐀-䶿"  # CJK extension A
    "一-鿿"  # CJK unified ideographs
    "가-힯"  # hangul syllables
    "豈-﫿"  # compatibility ideographs
    "＀-￯"  # halfwidth and fullwidth forms
)
CJK_RE = re.compile(f"[{CJK_RANGES}]")


def estimate_tokens(text: str) -> int:
    """Estimate the token count of a string.

    Args:
      text: The string to measure.

    Returns:
      An approximate token count, biased slightly high.
    """
    if not text:
        return 0
    cjk = len(CJK_RE.findall(text))
    latin = len(text) - cjk
    return int(cjk * TOKENS_PER_CHAR_CJK + latin / CHARS_PER_TOKEN_LATIN) + 1


def estimate_messages(messages: list[Message]) -> int:
    """Estimate the token cost of a message list, including tool calls."""
    from ..constants import ATTACHMENT_TOKEN_ESTIMATE
    from ..providers.base import message_text

    total = 0
    for message in messages:
        total += estimate_tokens(message_text(message.content)) + TOKENS_PER_MESSAGE_OVERHEAD
        attachments = message.meta.get("attachments") or []
        if isinstance(attachments, list):
            total += ATTACHMENT_TOKEN_ESTIMATE * len(attachments)
        for call in message.tool_calls:
            total += estimate_tokens(call.name) + estimate_tokens(call.arguments)
            total += TOKENS_PER_TOOL_CALL_OVERHEAD
    return total


def estimate_tools(tool_specs: list[dict]) -> int:
    """Estimate the token cost of the tool definitions block."""
    if not tool_specs:
        return 0
    return estimate_tokens(json.dumps(tool_specs))


@dataclass
class ContextMeter:
    """Tracks window occupancy, calibrated against reported usage."""

    window: int
    cfg: ContextConfig
    correction: float = 1.0
    last_reported: int = 0
    _samples: list[float] = field(default_factory=list)

    def calibrate(self, estimated: int, reported: int) -> None:
        """Fold a real ``prompt_tokens`` reading into the correction factor.

        Args:
          estimated: What :func:`estimate_messages` predicted for this request.
          reported: What the backend actually billed as prompt tokens.
        """
        if estimated <= 0 or reported <= 0:
            return
        self.last_reported = reported
        ratio = reported / estimated
        if not CALIBRATION_RATIO_MIN <= ratio <= CALIBRATION_RATIO_MAX:
            return  # cache accounting quirks and provider oddities
        self._samples.append(ratio)
        del self._samples[:-CALIBRATION_SAMPLE_WINDOW]
        self.correction = sum(self._samples) / len(self._samples)

    def used(self, messages: list[Message], tool_specs: list[dict] | None = None) -> int:
        raw = estimate_messages(messages) + estimate_tools(tool_specs or [])
        return int(raw * self.correction)

    def fraction(self, messages: list[Message], tool_specs: list[dict] | None = None) -> float:
        if self.window <= 0:
            return 0.0
        return self.used(messages, tool_specs) / self.window

    def should_compact(
        self, messages: list[Message], tool_specs: list[dict] | None = None
    ) -> bool:
        if not self.cfg.auto_compact:
            return False
        return self.fraction(messages, tool_specs) >= self.cfg.compact_threshold


@dataclass
class ContextSlice:
    """One labelled chunk of the context window."""

    name: str
    tokens: int
    #: Extra detail shown when the slice is expanded, e.g. per-server counts.
    detail: dict[str, int] = field(default_factory=dict)

    def share(self, window: int) -> float:
        return (self.tokens / window) if window else 0.0


@dataclass
class ContextBreakdown:
    """Where the context window actually went.

    A single "72% full" number tells you that you are in trouble but not why.
    Splitting it by origin is what makes it actionable: forty MCP tools you
    never call are a different problem from a transcript that genuinely needs
    compacting, and they have different fixes.
    """

    window: int
    slices: list[ContextSlice] = field(default_factory=list)

    @property
    def used(self) -> int:
        return sum(item.tokens for item in self.slices)

    @property
    def free(self) -> int:
        return max(self.window - self.used, 0)

    @property
    def fraction(self) -> float:
        return (self.used / self.window) if self.window else 0.0

    def largest(self) -> ContextSlice | None:
        return max(self.slices, key=lambda item: item.tokens, default=None)

    def rows(self) -> list[tuple[str, int, float]]:
        """Every slice plus free space, largest first, with free space last."""
        ordered = sorted(self.slices, key=lambda item: item.tokens, reverse=True)
        rows = [(item.name, item.tokens, item.share(self.window)) for item in ordered]
        rows.append(("Free space", self.free, self.free / self.window if self.window else 0.0))
        return rows


def measure_context(
    *,
    window: int,
    system_prompt: str,
    skills_section: str,
    messages: list[Message],
    tool_specs: list[dict],
    mcp_prefix: str = "mcp__",
    rules_section: str = "",
) -> ContextBreakdown:
    """Attribute the context window to the things that filled it.

    Args:
      window: The active model's context window.
      system_prompt: The full system prompt, skills section included.
      skills_section: The skills listing, so it can be counted separately
        from the instructions it is embedded in.
      messages: The conversation as it will be sent.
      tool_specs: Tool definitions, split into built-in and MCP by name.
      mcp_prefix: Prefix marking a tool as coming from an MCP server.
      rules_section: Global/project rules block counted separately.

    Returns:
      A :class:`ContextBreakdown` whose slices sum to the used total.
    """
    skill_tokens = estimate_tokens(skills_section)
    rule_tokens = estimate_tokens(rules_section)
    # Skills and rules live inside the system prompt; count each once.
    prompt_tokens = max(
        estimate_tokens(system_prompt) - skill_tokens - rule_tokens, 0
    )

    builtin_specs, mcp_specs, per_server = [], [], {}
    for spec in tool_specs:
        name = str((spec.get("function") or {}).get("name", ""))
        if name.startswith(mcp_prefix):
            mcp_specs.append(spec)
            server = name[len(mcp_prefix) :].split("__", maxsplit=1)[0] or "unknown"
            per_server[server] = per_server.get(server, 0) + estimate_tokens(
                json.dumps(spec)
            )
        else:
            builtin_specs.append(spec)

    conversation = [m for m in messages if m.role != "system"]
    slices = [
        ContextSlice("Messages", estimate_messages(conversation)),
        ContextSlice("System tools", estimate_tools(builtin_specs)),
        ContextSlice("MCP tools", estimate_tools(mcp_specs), detail=per_server),
        ContextSlice("System prompt", prompt_tokens),
        ContextSlice("Rules", rule_tokens),
        ContextSlice("Skills", skill_tokens),
    ]
    return ContextBreakdown(window=window, slices=[s for s in slices if s.tokens > 0])


def truncate_tool_result(text: str, limit: int) -> str:
    """Clip an over-long tool result, keeping the head and the tail.

    The middle is usually the least informative part of a long listing, and
    the tail often holds the error that matters.
    """
    if len(text) <= limit:
        return text
    head = text[: int(limit * TRUNCATE_HEAD_FRACTION)]
    tail = text[-int(limit * TRUNCATE_TAIL_FRACTION) :]
    elided = len(text) - len(head) - len(tail)
    return (
        f"{head}\n\n[... {elided} characters elided by the harness; "
        f"re-run with a narrower query if you need the full middle ...]\n\n{tail}"
    )


def _tail_chars(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return (
        f"[... {omitted} chars omitted; tail kept for the model ...]\n" + text[-limit:]
    )


_EXIT_RE = re.compile(r"exit(?:\s+code)?\s*[:=]?\s*(-?\d+)", re.IGNORECASE)
_PASSED_RE = re.compile(r"(\d+)\s+passed", re.IGNORECASE)
_FAILED_RE = re.compile(r"(\d+)\s+failed", re.IGNORECASE)
_ERROR_RE = re.compile(r"(\d+)\s+error", re.IGNORECASE)


def _bash_exit_code(text: str, *, is_error: bool) -> str | None:
    match = _EXIT_RE.search(text)
    if match:
        return match.group(1)
    return "1" if is_error else "0"


def _test_verdict_bits(text: str) -> list[str]:
    bits: list[str] = []
    for pattern, label in (
        (_PASSED_RE, "passed"),
        (_FAILED_RE, "failed"),
        (_ERROR_RE, "errors"),
    ):
        match = pattern.search(text)
        if match:
            bits.append(f"{match.group(1)} {label}")
    return bits


def _short_command(command: str, limit: int = 120) -> str:
    compact = " ".join(command.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"


def summarize_tool_result(
    tool_name: str,
    text: str,
    *,
    is_error: bool = False,
    command: str = "",
    max_chars: int = PRUNE_DIGEST_CHARS,
) -> str:
    """Heuristic one-line status for prune digests and headers."""
    name = tool_name or "tool"
    body = (text or "").strip()
    parts: list[str] = [name]

    if name == "Bash" or command:
        exit_code = _bash_exit_code(body, is_error=is_error)
        if exit_code is not None:
            parts.append(f"exit={exit_code}")
        parts.extend(_test_verdict_bits(body))
        if command:
            parts.append(f"cmd: {_short_command(command)}")
    elif name in {"Write", "Edit"}:
        first = body.split("\n", 1)[0].strip()
        parts.append(first or ("error" if is_error else "ok"))
    elif name in {"Read", "Grep", "Glob"}:
        lines = body.count("\n") + (1 if body else 0)
        parts.append(f"{lines} lines" if body else "empty")
        if is_error:
            parts.append("error")
    else:
        verdict = _test_verdict_bits(body)
        if verdict:
            parts.extend(verdict)
        elif is_error:
            parts.append("error")
        else:
            parts.append("ok")
        first = body.split("\n", 1)[0].strip()
        if first and first.lower() not in {"ok", "error"}:
            parts.append(_short_command(first, 80))

    summary = " · ".join(p for p in parts if p)
    if len(summary) > max_chars:
        summary = summary[: max_chars - 1] + "…"
    return summary


def prepare_tool_result_for_model(
    tool_name: str,
    text: str,
    *,
    is_error: bool = False,
    command: str = "",
    context: ContextConfig | None = None,
) -> str:
    """Shape a tool result for the main model (keep status, drop bulk).

    The durable log should store the pre-digest text in ``meta["full"]`` when
    this returns a shorter string. Live GUI cards still use the original
    :class:`ToolResult` from the tool runner.
    """
    cfg = context or ContextConfig()
    raw = text or ""
    name = tool_name or ""

    if name == "Bash":
        limit = cfg.bash_error_chars if is_error else cfg.bash_success_chars
        if limit <= 0:
            limit = BASH_ERROR_RESULT_CHARS if is_error else BASH_SUCCESS_RESULT_CHARS
        exit_code = _bash_exit_code(raw, is_error=is_error) or ("1" if is_error else "0")
        header = f"[Bash] exit={exit_code}"
        if command:
            header += f" · cmd: {_short_command(command, 160)}"
        verdict = _test_verdict_bits(raw)
        if verdict:
            header += " · " + ", ".join(verdict)
        # Drop a redundant "exit code N" line from the body when present.
        body = raw
        if body.lower().startswith("exit code"):
            body = body.split("\n", 1)[1].lstrip("\n") if "\n" in body else ""
        body = _tail_chars(body.strip() or "(no output)", limit)
        return f"{header}\n\n{body}"

    if name in {"Read", "Grep", "Glob"}:
        limit = min(cfg.read_result_chars or READ_RESULT_CHARS, cfg.max_tool_result_chars)
        return truncate_tool_result(raw, limit)

    if name in {"Write", "Edit"}:
        # Already short status lines from the tools; keep as-is.
        return raw

    # Verify / workflow / MCP: prefer tail + test counters when bulky.
    if len(raw) > cfg.max_tool_result_chars:
        header = summarize_tool_result(
            name, raw, is_error=is_error, command=command, max_chars=160
        )
        body = _tail_chars(raw, min(cfg.bash_error_chars, cfg.max_tool_result_chars))
        return f"[{header}]\n\n{body}"
    return raw


#: Legacy stub text; prefer :func:`make_prune_digest`.
PRUNED_TOOL_STUB = (
    "[digest] earlier tool output pruned — call again only if you need details"
)


def microcompact_reads(
    messages: list[Message],
    *,
    keep_recent: int = 3,
) -> int:
    """Collapse older Read results when the same path was read again later.

    Keeps the newest ``keep_recent`` Read outputs per path; older ones become
    a one-line stub. Runs before prune so duplicate file dumps never reach
    the main model. Mutates ``messages`` in place.
    """
    if not messages or keep_recent < 1:
        return 0
    seen: dict[str, int] = {}
    compacted = 0
    for message in reversed(messages):
        if message.role != "tool":
            continue
        tool_name = str(message.meta.get("tool") or message.name or "")
        if tool_name != "Read":
            continue
        if message.meta.get("microcompacted") or message.meta.get("pruned"):
            continue
        if message.meta.get("cached_read"):
            continue
        path = str(message.meta.get("path") or "").strip()
        if not path:
            # Best-effort: first line of a normal Read looks like "12\tcode".
            continue
        count = seen.get(path, 0)
        seen[path] = count + 1
        if count < keep_recent:
            continue
        text = message.content if isinstance(message.content, str) else str(message.content)
        if "full" not in message.meta and text:
            message.meta = {**message.meta, "full": text}
        message.content = (
            f"[microcompact] Read {path} — earlier contents omitted "
            "(a newer Read of this path is in context)."
        )
        message.meta = {**message.meta, "microcompacted": True}
        compacted += 1
    return compacted


def make_prune_digest(
    tool_name: str,
    text: str,
    *,
    is_error: bool = False,
    command: str = "",
) -> str:
    """Replace a bulky tool result with a status line the next turn can use."""
    summary = summarize_tool_result(
        tool_name,
        text,
        is_error=is_error,
        command=command,
        max_chars=PRUNE_DIGEST_CHARS,
    )
    return (
        f"[digest] {summary}. "
        "Full output omitted to free context; re-run the tool only if needed."
    )


def prune_old_tool_outputs(
    messages: list[Message],
    *,
    protect_tokens: int = PRUNE_PROTECT_TOKENS,
    minimum_tokens: int = PRUNE_MINIMUM_TOKENS,
    keep_user_turns: int = PRUNE_KEEP_USER_TURNS,
    protected_tools: frozenset[str] = PRUNE_PROTECTED_TOOLS,
) -> int:
    """Replace older tool results with a short digest (OpenCode-style prune).

    Walks backwards from the end of the transcript, keeps roughly
    ``protect_tokens`` of recent tool output, and digests anything older once
    reclaiming at least ``minimum_tokens``. Mutates ``messages`` in place
    (wire/memory view; the durable session log is not rewritten).

    Returns:
      How many tool messages were pruned.
    """
    if not messages:
        return 0

    candidates: list[tuple[Message, int]] = []
    protected = 0
    user_turns = 0
    for message in reversed(messages):
        if message.role == "user" and not message.meta.get("compacted"):
            user_turns += 1
        if user_turns < keep_user_turns:
            continue
        if message.role != "tool":
            continue
        if message.meta.get("pruned"):
            continue
        tool_name = str(message.meta.get("tool") or message.name or "")
        if tool_name in protected_tools:
            continue
        text = message.content if isinstance(message.content, str) else str(message.content)
        tokens = estimate_tokens(text)
        if tokens <= 0:
            continue
        protected += tokens
        if protected <= protect_tokens:
            continue
        candidates.append((message, tokens))

    reclaimed = sum(tokens for _, tokens in candidates)
    if reclaimed < minimum_tokens:
        return 0

    for message, _tokens in candidates:
        tool_name = str(message.meta.get("tool") or message.name or "")
        source = str(message.meta.get("full") or message.content or "")
        command = str(message.meta.get("command") or "")
        is_error = bool(message.meta.get("is_error"))
        if "full" not in message.meta and isinstance(message.content, str):
            # Preserve pre-prune text for audit when we only had the wire copy.
            message.meta = {**message.meta, "full": message.content}
        message.content = make_prune_digest(
            tool_name, source, is_error=is_error, command=command
        )
        message.meta = {**message.meta, "pruned": True}
    return len(candidates)


COMPACT_PROMPT = """\
You are compacting a coding-agent transcript so work can continue in a fresh \
context window. Produce a dense handoff note. Another agent will read ONLY your \
note plus the last few messages — anything you omit is lost to it.

Cover, in this order, with concrete detail:

1. **Goal** — what the user asked for, in their own terms, including any \
constraints or preferences they stated.
2. **State** — what has been done so far. Name every file created or modified \
with its path, and say what changed in each.
3. **Findings** — facts about the codebase that were expensive to learn: \
architecture, conventions, gotchas, where things live, the build and test \
commands.
4. **Decisions** — choices made and why, including approaches tried and \
rejected, so they are not retried.
5. **Open** — what is unfinished, what was about to happen next, and any errors \
or failing tests currently outstanding.

Be specific: exact paths, function names, commands, error strings. Do not \
editorialise and do not summarise your own summary. If the user gave explicit \
instructions that still apply, quote them verbatim.
"""


def _split_for_compaction(
    messages: list[Message],
    keep_recent: int,
    *,
    preserve_recent_tokens: int = 0,
) -> tuple[list[Message], list[Message], list[Message]]:
    """Partition a transcript into (system, older, recent).

    Starts from ``keep_recent`` messages, then grows the recent tail while
    estimated tokens stay under ``preserve_recent_tokens``. The recent tail
    is trimmed so it never begins with a tool result whose originating
    assistant call would be compacted away — backends reject that shape.
    """
    system = [m for m in messages if m.role == "system"]
    body = [m for m in messages if m.role != "system"]

    keep = max(keep_recent, COMPACT_MIN_MESSAGES)
    if len(body) <= keep + COMPACT_MIN_MESSAGES:
        return system, [], body

    recent_count = keep
    if preserve_recent_tokens > 0:
        total = 0
        for index in range(len(body) - 1, -1, -1):
            total += estimate_messages([body[index]])
            span = len(body) - index
            if span < keep:
                continue
            if total > preserve_recent_tokens and span > keep:
                break
            recent_count = span

    recent = body[-recent_count:]
    while recent and recent[0].role == "tool":
        recent = recent[1:]
    older = body[: len(body) - len(recent)]
    return system, older, recent


async def compact(
    messages: list[Message],
    router: Router,
    selection: Selection,
    cfg: ContextConfig,
    window: int,
) -> tuple[list[Message], str]:
    """Replace the older part of a transcript with an LLM-written summary.

    Args:
      messages: The current wire messages, including the system prompt.
      router: Router used to reach the compactor model.
      selection: Which model and account performs the compaction.
      cfg: Context settings controlling how much is kept.
      window: The active model's context window, used to size the summary.

    Returns:
      A tuple of the new message list and the summary text. When compaction
      was not worthwhile, the original list and an empty string are returned.
    """
    system, older, recent = _split_for_compaction(
        messages,
        cfg.keep_recent_messages,
        preserve_recent_tokens=cfg.preserve_recent_tokens,
    )
    if not older:
        return messages, ""

    budget = max(int(window * cfg.summary_budget), COMPACT_SUMMARY_MIN_TOKENS)
    reply = await router.ask(
        selection,
        [
            Message(role="system", content=COMPACT_PROMPT),
            Message(
                role="user",
                content=(
                    "Transcript to compact:\n\n<transcript>\n"
                    f"{render_transcript(older)}\n</transcript>"
                ),
            ),
        ],
        role="compactor",
        max_tokens=budget,
    )

    summary = (reply.message.content or "").strip()
    if not summary:
        return messages, ""

    marker = Message(
        role="user",
        content=(
            "[Earlier turns were compacted. The handoff note below replaces "
            "them; the full transcript is still on disk. Continue from here.]"
            f"\n\n{summary}"
        ),
        meta={"compacted": True, "replaced": len(older)},
    )
    ack = Message(
        role="assistant",
        content="Understood — continuing from the handoff note.",
        meta={"compacted": True},
    )
    return system + [marker, ack] + recent, summary


def render_transcript(messages: list[Message]) -> str:
    """Flatten messages into plain text for the compactor to read."""
    from ..providers.base import message_text

    parts: list[str] = []
    for message in messages:
        text = message_text(message.content)
        if message.role == "tool":
            parts.append(f"[tool result] {text[:COMPACT_TOOL_RESULT_CHARS]}")
            continue
        if message.role == "assistant":
            if text:
                parts.append(f"[assistant] {text[:COMPACT_MESSAGE_CHARS]}")
            for call in message.tool_calls:
                arguments = call.arguments[:COMPACT_TOOL_ARGS_CHARS]
                parts.append(f"[assistant calls {call.name}] {arguments}")
            continue
        parts.append(f"[{message.role}] {text[:COMPACT_MESSAGE_CHARS]}")
    return "\n\n".join(parts)
