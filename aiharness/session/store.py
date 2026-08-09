"""Durable session storage.

The guarantee this module provides: **a message that was accepted is never
lost until the user explicitly deletes the session.**

That is achieved by separating two things people usually conflate:

* the *record* — every message ever exchanged, appended to ``messages.jsonl``
  and never rewritten;
* the *view* — the subset of the record actually sent to the model, which
  shrinks when the context is compacted.

Compaction writes a marker into ``compactions.jsonl`` saying "messages 0..N
are represented by this summary". Rebuilding the view replays the record and
applies the markers. Dropping the markers restores the full history verbatim,
which is what ``/uncompact`` does.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from platformdirs import user_data_dir

from ..constants import (
    INTERRUPTED_TOOL_RESULT,
    SESSION_LIST_LIMIT,
    SESSION_SCHEMA_VERSION,
    SESSION_TITLE_CHARS,
)
from ..providers.base import Message, ToolCall

MESSAGES_FILE = "messages.jsonl"
COMPACTIONS_FILE = "compactions.jsonl"
META_FILE = "meta.json"
TODOS_FILE = "todos.json"


def sessions_root() -> Path:
    """Return the directory holding all persisted sessions."""
    override = os.environ.get("AIH_SESSION_DIR")
    if override:
        return Path(override).expanduser()
    return Path(user_data_dir("aiharness", appauthor=False)) / "sessions"


# --------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------


@dataclass
class SessionMeta:
    """Descriptive header for one session."""

    id: str
    workspace: str
    created_at: float
    updated_at: float
    title: str = ""
    model: str = ""
    account: str = ""
    #: Last ask/auto/yolo choice for this chat. Empty = inherit config default.
    permission_mode: str = ""
    message_count: int = 0
    total_cost: float = 0.0
    #: Cumulative prompt tokens seen while this session was open (durable).
    cache_prompt_tokens: int = 0
    #: Cumulative tokens reported as cache hits for this session (durable).
    cache_cached_tokens: int = 0
    #: Archived sessions are kept on disk but hidden from the normal list.
    #: Archiving is not deletion: the point is to clear the sidebar without
    #: destroying anything, so it stays reversible.
    archived: bool = False
    schema_version: int = SESSION_SCHEMA_VERSION

    @property
    def empty(self) -> bool:
        return self.message_count == 0

    @property
    def cache_hit_rate(self) -> float:
        if self.cache_prompt_tokens <= 0:
            return 0.0
        return self.cache_cached_tokens / self.cache_prompt_tokens

    @property
    def workspace_name(self) -> str:
        """The last path component, which is what people call the project."""
        return Path(self.workspace).name or self.workspace

    @property
    def created_label(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.created_at))

    @property
    def updated_label(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.updated_at))


@dataclass
class CompactionRecord:
    """A note that a prefix of the record is represented by a summary."""

    at: float
    replaced_through: int  # exclusive index into the message record
    summary: str
    model: str = ""
    tokens_before: int = 0
    tokens_after: int = 0


def pair_tool_calls(
    messages: list[Message],
    *,
    filler: str = INTERRUPTED_TOOL_RESULT,
) -> list[Message]:
    """Return a copy where every ``tool_calls`` entry has a tool result.

    Providers require each assistant ``tool_call_id`` to be answered by a
    following ``role=tool`` message before any other role appears. An
    interrupt (or a hard task cancel) can leave the record one step short of
    that, and the next user prompt then dies with HTTP 400.

    Also drops misplaced ``role=tool`` rows (after a user turn, or unbound
    ids). DeepSeek rejects those with the reverse 400 — tool without a
    preceding ``tool_calls`` owner. The durable record stays untouched; this
    only shapes what the model is sent.
    """
    repaired: list[Message] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        index += 1
        # Orphan / late tool results must never reach the provider alone.
        if message.role == "tool":
            continue
        repaired.append(message)
        if message.role != "assistant" or not message.tool_calls:
            continue
        owner_ids = {call.id for call in message.tool_calls}
        seen: set[str] = set()
        while index < len(messages) and messages[index].role == "tool":
            tool_msg = messages[index]
            index += 1
            tool_id = tool_msg.tool_call_id or ""
            if tool_id in owner_ids and tool_id not in seen:
                repaired.append(tool_msg)
                seen.add(tool_id)
        for call in message.tool_calls:
            if call.id in seen:
                continue
            repaired.append(
                Message(
                    role="tool",
                    content=filler,
                    tool_call_id=call.id,
                    name=call.name,
                    meta={
                        "is_error": True,
                        "tool": call.name,
                        "synthetic": True,
                    },
                )
            )
            seen.add(call.id)
    return repaired


def unanswered_tool_calls(messages: list[Message]) -> list[ToolCall]:
    """Tool calls that still lack a contiguous ``role=tool`` reply.

    Only results that immediately follow their owning assistant count as
    answers — a tool row after a later user message does not close the call.
    """
    pending: list[ToolCall] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        index += 1
        if message.role != "assistant" or not message.tool_calls:
            continue
        owner_ids = {call.id for call in message.tool_calls}
        seen: set[str] = set()
        while index < len(messages) and messages[index].role == "tool":
            tool_id = messages[index].tool_call_id or ""
            if tool_id in owner_ids:
                seen.add(tool_id)
            index += 1
        for call in message.tool_calls:
            if call.id not in seen:
                pending.append(call)
    return pending


def message_to_record(message: Message) -> dict[str, Any]:
    """Serialise a message for the append-only log."""
    record: dict[str, Any] = {
        "role": message.role,
        "content": message.content,
        "at": time.time(),
    }
    if message.reasoning:
        record["reasoning"] = message.reasoning
    if message.tool_call_id:
        record["tool_call_id"] = message.tool_call_id
    if message.name:
        record["name"] = message.name
    if message.tool_calls:
        record["tool_calls"] = [
            {"id": call.id, "name": call.name, "arguments": call.arguments}
            for call in message.tool_calls
        ]
    if message.meta:
        record["meta"] = message.meta
    return record


def record_to_message(record: dict[str, Any]) -> Message:
    """Rebuild a message from its logged form."""
    return Message(
        role=record.get("role", "user"),
        content=record.get("content", "") or "",
        reasoning=record.get("reasoning", "") or "",
        tool_call_id=record.get("tool_call_id"),
        name=record.get("name"),
        tool_calls=[
            ToolCall(
                id=call.get("id", ""),
                name=call.get("name", ""),
                arguments=call.get("arguments", "") or "",
            )
            for call in record.get("tool_calls", [])
        ],
        meta=record.get("meta", {}) or {},
    )


# --------------------------------------------------------------------------
# one session
# --------------------------------------------------------------------------


class SessionHandle:
    """Read/write access to a single persisted session."""

    def __init__(self, directory: Path, meta: SessionMeta):
        self.directory = directory
        self.meta = meta
        self._messages: list[Message] = []
        self._compactions: list[CompactionRecord] = []
        self._loaded = False
        #: TodoWrite list for this chat (durable via ``todos.json``).
        self.todos: list[dict] = []

    # -- paths ------------------------------------------------------------

    @property
    def messages_path(self) -> Path:
        return self.directory / MESSAGES_FILE

    @property
    def compactions_path(self) -> Path:
        return self.directory / COMPACTIONS_FILE

    @property
    def meta_path(self) -> Path:
        return self.directory / META_FILE

    @property
    def todos_path(self) -> Path:
        return self.directory / TODOS_FILE

    # -- loading ----------------------------------------------------------

    def load(self) -> SessionHandle:
        """Read the full record from disk. Idempotent."""
        self._messages = list(self._iter_messages())
        self._compactions = list(self._iter_compactions())
        self.todos = self._read_todos()
        self._loaded = True
        return self

    def _read_todos(self) -> list[dict]:
        path = self.todos_path
        if not path.is_file():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict) and item.get("content")]

    def save_todos(self, todos: list[dict] | None = None) -> None:
        """Persist the chat's TodoWrite list atomically."""
        if todos is not None:
            self.todos = list(todos)
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.todos_path
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self.todos, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(path)

    def _iter_messages(self) -> Iterator[Message]:
        if not self.messages_path.exists():
            return
        with self.messages_path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                try:
                    yield record_to_message(json.loads(line))
                except json.JSONDecodeError:
                    continue  # a torn final line must not lose the rest

    def _iter_compactions(self) -> Iterator[CompactionRecord]:
        if not self.compactions_path.exists():
            return
        with self.compactions_path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                yield CompactionRecord(
                    at=payload.get("at", 0.0),
                    replaced_through=payload.get("replaced_through", 0),
                    summary=payload.get("summary", ""),
                    model=payload.get("model", ""),
                    tokens_before=payload.get("tokens_before", 0),
                    tokens_after=payload.get("tokens_after", 0),
                )

    # -- accessors --------------------------------------------------------

    @property
    def full_history(self) -> list[Message]:
        """Every message ever recorded, in order, uncompacted."""
        if not self._loaded:
            self.load()
        return list(self._messages)

    @property
    def compactions(self) -> list[CompactionRecord]:
        if not self._loaded:
            self.load()
        return list(self._compactions)

    def view(self) -> list[Message]:
        """The messages that should actually be sent to the model.

        Applies the most recent compaction marker: everything before its
        boundary is replaced by the summary pair, and everything after it is
        replayed verbatim.
        """
        if not self._loaded:
            self.load()
        if not self._compactions:
            return list(self._messages)

        latest = self._compactions[-1]
        boundary = max(0, min(latest.replaced_through, len(self._messages)))
        tail = self._messages[boundary:]
        # A tool result whose originating call was compacted away would be
        # rejected by the API, so skip any orphans at the head of the tail.
        while tail and tail[0].role == "tool":
            tail = tail[1:]
        return _summary_pair(latest) + tail

    # -- writing ----------------------------------------------------------

    def append(self, message: Message) -> None:
        """Append one message to the durable record and the in-memory copy."""
        if not self._loaded:
            self.load()
        self._messages.append(message)
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.messages_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(message_to_record(message), ensure_ascii=False) + "\n")
            handle.flush()
        self.meta.message_count = len(self._messages)
        self.meta.updated_at = time.time()
        if not self.meta.title and message.role == "user":
            # Prefer what the user actually typed. The stored content also
            # carries the <environment> note stamped on for cache stability,
            # and titling every session "<environment> Date: ..." helps nobody.
            from ..providers.base import message_text

            self.meta.title = _derive_title(
                message.meta.get("user_text") or message_text(message.content)
            )
        self.save_meta()

    def append_many(self, messages: list[Message]) -> None:
        for message in messages:
            self.append(message)

    def record_compaction(self, record: CompactionRecord) -> None:
        """Record that a prefix of the history is now represented by a summary."""
        if not self._loaded:
            self.load()
        self._compactions.append(record)
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.compactions_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
            handle.flush()
        self.meta.updated_at = time.time()
        self.save_meta()

    def drop_compactions(self) -> int:
        """Discard every compaction marker, restoring the full history.

        Returns:
          How many markers were removed.
        """
        if not self._loaded:
            self.load()
        removed = len(self._compactions)
        self._compactions.clear()
        self.compactions_path.unlink(missing_ok=True)
        self.meta.updated_at = time.time()
        self.save_meta()
        return removed

    def save_meta(self) -> None:
        """Write meta atomically so a killed process cannot leave torn JSON."""
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.meta_path
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(asdict(self.meta), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)

    def set_model(self, model: str, account: str = "") -> None:
        self.meta.model = model
        self.meta.account = account
        self.save_meta()

    def set_permission_mode(self, mode: str) -> None:
        self.meta.permission_mode = mode
        self.save_meta()

    def add_cost(self, amount: float) -> None:
        self.meta.total_cost += amount
        self.save_meta()

    def add_cache(self, prompt_tokens: int, cached_tokens: int) -> None:
        """Accumulate durable cache counters for this session."""
        self.meta.cache_prompt_tokens += max(0, int(prompt_tokens))
        self.meta.cache_cached_tokens += max(0, int(cached_tokens))
        self.save_meta()

    def rename(self, title: str) -> None:
        self.meta.title = title.strip()[:SESSION_TITLE_CHARS]
        self.save_meta()

    # -- destructive ------------------------------------------------------

    def clear_messages(self) -> None:
        """Erase the conversation but keep the session and its identity.

        This is a real deletion: the append-only log is removed, not marked.
        """
        self._messages.clear()
        self._compactions.clear()
        self.messages_path.unlink(missing_ok=True)
        self.compactions_path.unlink(missing_ok=True)
        self.meta.message_count = 0
        self.meta.total_cost = 0.0
        self.meta.cache_prompt_tokens = 0
        self.meta.cache_cached_tokens = 0
        self.meta.updated_at = time.time()
        self.save_meta()

    def truncate_from(self, index: int) -> int:
        """Drop messages from ``index`` inclusive and rewrite the log.

        Used to rewind a conversation to before a chosen user turn. Compaction
        markers that sit at or past the cut are discarded so the view cannot
        reference missing history.

        Args:
          index: First message index to remove (0-based in full history).

        Returns:
          How many messages were removed.
        """
        if not self._loaded:
            self.load()
        if index < 0 or index >= len(self._messages):
            return 0
        removed = len(self._messages) - index
        self._messages = self._messages[:index]
        self._compactions = [
            record
            for record in self._compactions
            if record.replaced_through <= index
        ]
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.messages_path.open("w", encoding="utf-8") as handle:
            for message in self._messages:
                handle.write(
                    json.dumps(message_to_record(message), ensure_ascii=False) + "\n"
                )
        if self._compactions:
            with self.compactions_path.open("w", encoding="utf-8") as handle:
                for record in self._compactions:
                    handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
        else:
            self.compactions_path.unlink(missing_ok=True)
        self.meta.message_count = len(self._messages)
        self.meta.updated_at = time.time()
        self.save_meta()
        return removed

    def delete(self) -> None:
        """Remove the session directory and everything in it."""
        shutil.rmtree(self.directory, ignore_errors=True)


def _summary_pair(record: CompactionRecord) -> list[Message]:
    """The two messages that stand in for a compacted prefix."""
    marker = Message(
        role="user",
        content=(
            "[Earlier turns were compacted. The handoff note below replaces "
            "them; the full transcript is still on disk. Continue from here.]"
            f"\n\n{record.summary}"
        ),
        meta={"compacted": True, "replaced_through": record.replaced_through},
    )
    ack = Message(
        role="assistant",
        content="Understood — continuing from the handoff note.",
        meta={"compacted": True},
    )
    return [marker, ack]


def _derive_title(text: str) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= SESSION_TITLE_CHARS:
        return cleaned
    return cleaned[: SESSION_TITLE_CHARS - 1] + "…"


# --------------------------------------------------------------------------
# the store
# --------------------------------------------------------------------------


class SessionStore:
    """Creates, lists and deletes sessions under a root directory."""

    def __init__(self, root: Path | None = None):
        self.root = root or sessions_root()

    def create(
        self,
        workspace: Path,
        *,
        model: str = "",
        account: str = "",
        permission_mode: str = "",
    ) -> SessionHandle:
        session_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        now = time.time()
        meta = SessionMeta(
            id=session_id,
            workspace=str(workspace),
            created_at=now,
            updated_at=now,
            model=model,
            account=account,
            permission_mode=permission_mode,
        )
        directory = self.root / session_id
        directory.mkdir(parents=True, exist_ok=True)
        handle = SessionHandle(directory, meta)
        handle.save_meta()
        handle._loaded = True
        return handle

    def open(self, session_id: str) -> SessionHandle | None:
        directory = self.root / session_id
        meta = self._read_meta(directory)
        if meta is None:
            return None
        return SessionHandle(directory, meta).load()

    def latest(self, workspace: Path | None = None) -> SessionHandle | None:
        entries = self.list(workspace=workspace, limit=1)
        if not entries:
            return None
        return self.open(entries[0].id)

    def list(
        self,
        *,
        workspace: Path | None = None,
        limit: int = SESSION_LIST_LIMIT,
        include_archived: bool = False,
        include_empty: bool = True,
        keep: str = "",
    ) -> list[SessionMeta]:
        """List sessions, newest first.

        Args:
          workspace: Restrict to one project directory.
          limit: Maximum returned.
          include_archived: Whether archived sessions are listed.
          include_empty: Whether sessions with no messages are listed. They
            are noise in a sidebar — an empty session looks identical to
            every other empty session.
          keep: A session id always included, whatever the filters say. This
            is how the session you are currently looking at stays visible
            even when it is still empty.
        """
        if not self.root.is_dir():
            return []
        found: list[SessionMeta] = []
        for directory in self.root.iterdir():
            if not directory.is_dir():
                continue
            meta = self._read_meta(directory)
            if meta is None:
                continue
            if meta.id != keep:
                if workspace is not None and Path(meta.workspace) != Path(workspace):
                    continue
                if meta.archived and not include_archived:
                    continue
                if meta.empty and not include_empty:
                    continue
            found.append(meta)
        found.sort(key=lambda m: m.updated_at, reverse=True)
        return found[:limit]

    def workspaces(self, limit: int = SESSION_LIST_LIMIT) -> list[tuple[Path, int]]:
        """Every project directory that has sessions, most recent first.

        Returns:
          Pairs of ``(path, session_count)``.
        """
        seen: dict[str, list[float | int]] = {}
        for meta in self.list(limit=10**6, include_empty=False):
            entry = seen.setdefault(meta.workspace, [0.0, 0])
            entry[0] = max(entry[0], meta.updated_at)
            entry[1] = int(entry[1]) + 1
        ordered = sorted(seen.items(), key=lambda kv: kv[1][0], reverse=True)
        return [(Path(path), int(count)) for path, (_, count) in ordered][:limit]

    def set_archived(self, session_id: str, archived: bool) -> bool:
        """Archive or restore one session."""
        handle = self.open(session_id)
        if handle is None:
            return False
        handle.meta.archived = archived
        handle.save_meta()
        return True

    def prune_empty(self, *, keep: str = "", workspace: Path | None = None) -> int:
        """Delete sessions that never received a message.

        Opening the app creates a session before anyone has typed anything,
        so without this the sidebar slowly fills with identical untitled
        entries that cannot be told apart.

        Returns:
          How many were removed.
        """
        removed = 0
        for meta in self.list(workspace=workspace, limit=10**6, include_archived=True):
            if meta.empty and meta.id != keep and self.delete(meta.id):
                removed += 1
        return removed

    def delete(self, session_id: str) -> bool:
        directory = self.root / session_id
        if not directory.is_dir():
            return False
        shutil.rmtree(directory, ignore_errors=True)
        return True

    def delete_all(self, workspace: Path | None = None) -> int:
        """Delete every session, or every session for one workspace.

        Returns:
          How many sessions were removed.
        """
        removed = 0
        for meta in self.list(workspace=workspace, limit=0 or 10**9):
            if self.delete(meta.id):
                removed += 1
        return removed

    def _read_meta(self, directory: Path) -> SessionMeta | None:
        path = directory / META_FILE
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        try:
            return SessionMeta(**{k: v for k, v in payload.items() if k in SessionMeta.__annotations__})
        except TypeError:
            return None
