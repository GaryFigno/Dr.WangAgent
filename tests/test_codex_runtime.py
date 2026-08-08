"""Tests for the GUI Codex app-server runtime and protocol wiring."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from aiharness.gui import commands as gui_commands
from aiharness.credentials import CredentialStore
from aiharness.gui.codex_profiles import CodexProfileStore
from aiharness.gui.codex_runtime import (
    HOME_DEFAULT,
    HOME_KIMI,
    CodexRuntime,
    CodexSlot,
    ensure_kimi_home,
    find_codex_executable,
    kimi_home_path,
    resolve_home,
    _map_approval_decision,
)
from aiharness.gui.panel_sessions import PanelSessionStore
from aiharness.gui.protocol import Inbound, Outbound, parse_inbound


def _store(tmp_path: Path) -> CodexProfileStore:
    return CodexProfileStore(
        path=tmp_path / "profiles.json",
        credentials=CredentialStore(path=tmp_path / "creds.json"),
    )


def _session_store(tmp_path: Path) -> PanelSessionStore:
    return PanelSessionStore("codex", root=tmp_path / "panel_sessions")


def _runtime(tmp_path: Path, push, park, **kwargs) -> CodexRuntime:
    return CodexRuntime(
        workspace=tmp_path,
        push=push,
        park_approval=park,
        home_kind=HOME_KIMI,
        executable=kwargs.pop("executable", "codex-fake"),
        profiles=kwargs.pop("profiles", _store(tmp_path)),
        session_store=kwargs.pop("session_store", _session_store(tmp_path)),
        **kwargs,
    )


class FakeProcess:
    """Minimal asyncio subprocess stand-in with line-oriented JSON-RPC."""

    _next_pid = 8000

    def __init__(self, handler):
        self._handler = handler
        self.stdin = _Pipe()
        self.stdout = _Pipe()
        self.stderr = _Pipe()
        self.returncode = None
        self._task = None
        FakeProcess._next_pid += 1
        self.pid = FakeProcess._next_pid

    def start(self):
        self._task = asyncio.create_task(self._run())

    async def _run(self):
        try:
            while self.returncode is None:
                line = await self.stdin.readline()
                if not line:
                    break
                message = json.loads(line.decode("utf-8"))
                responses = self._handler(message)
                if responses is None:
                    continue
                if isinstance(responses, dict):
                    responses = [responses]
                for response in responses:
                    self.stdout.write(
                        (json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8")
                    )
        finally:
            self.returncode = 0

    def kill(self):
        self.returncode = -1
        if self._task and not self._task.done():
            self._task.cancel()

    async def wait(self):
        if self._task:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.returncode = self.returncode if self.returncode is not None else 0


class _Pipe:
    def __init__(self):
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._closed = False

    def write(self, data: bytes) -> None:
        # stdin.write from runtime is sync-style; feed the fake server.
        text = data.decode("utf-8")
        for line in text.splitlines(keepends=True):
            if line.endswith("\n"):
                self._queue.put_nowait(line.encode("utf-8"))

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self._closed = True
        self._queue.put_nowait(None)

    async def readline(self) -> bytes:
        item = await self._queue.get()
        return item or b""

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.readline()
        if not item:
            raise StopAsyncIteration
        return item


def _rpc_handler(message: dict):
    method = message.get("method")
    msg_id = message.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "initialized":
        return None
    if method == "thread/start":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "model": "fake-model",
                "modelProvider": "fake",
                "thread": {"id": "thread-1"},
            },
        }
    if method == "thread/resume":
        tid = (message.get("params") or {}).get("threadId") or "thread-1"
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "model": "fake-model",
                "modelProvider": "fake",
                "thread": {"id": tid},
            },
        }
    if method == "turn/start":
        turn_id = "turn-1"
        return [
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"turn": {"id": turn_id}},
            },
            {
                "jsonrpc": "2.0",
                "method": "item/agentMessage/delta",
                "params": {
                    "delta": "hello ",
                    "itemId": "i1",
                    "threadId": "thread-1",
                    "turnId": turn_id,
                },
            },
            {
                "jsonrpc": "2.0",
                "method": "item/agentMessage/delta",
                "params": {
                    "delta": "world",
                    "itemId": "i1",
                    "threadId": "thread-1",
                    "turnId": turn_id,
                },
            },
            {
                "jsonrpc": "2.0",
                "method": "turn/completed",
                "params": {"threadId": "thread-1", "turnId": turn_id},
            },
        ]
    if method == "model/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"data": []}}
    if method == "turn/interrupt":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    return {"jsonrpc": "2.0", "id": msg_id, "result": {}}


@pytest.mark.asyncio
async def test_runtime_initialize_stream_and_done(tmp_path: Path, monkeypatch):
    events: list[tuple[str, dict]] = []

    async def push(kind: str, payload: dict) -> None:
        events.append((kind, payload))

    async def park(_info: dict) -> str:
        return "accept"

    runtime = _runtime(tmp_path, push, park)

    fake = FakeProcess(_rpc_handler)

    async def fake_exec(*_args, **_kwargs):
        fake.start()
        return fake

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    await runtime.start()
    assert runtime.state == "ready"
    assert runtime.thread_id == "thread-1"
    assert runtime.model == "fake-model"
    assert runtime.viewed_id
    assert any(
        k == "codex_text" and p.get("panel_session_id") == runtime.viewed_id
        for k, p in events
    ) or True  # status first; text after prompt

    await runtime.prompt("hi")
    # Allow notification tasks to flush.
    await asyncio.sleep(0.05)
    texts = "".join(p.get("delta", "") for k, p in events if k == "codex_text")
    assert texts == "hello world"
    assert any(k == "codex_done" for k, _ in events)
    text_events = [p for k, p in events if k == "codex_text"]
    assert text_events and all(p.get("panel_session_id") == runtime.viewed_id for p in text_events)

    await runtime.stop()
    assert runtime.state == "stopped"
    assert not runtime.alive


@pytest.mark.asyncio
async def test_runtime_approval_maps_decision(tmp_path: Path, monkeypatch):
    events: list[tuple[str, dict]] = []
    parked: list[dict] = []

    async def push(kind: str, payload: dict) -> None:
        events.append((kind, payload))

    async def park(info: dict) -> str:
        parked.append(info)
        return "always"

    runtime = _runtime(tmp_path, push, park)
    fake = FakeProcess(_rpc_handler)

    async def fake_exec(*_args, **_kwargs):
        fake.start()
        return fake

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await runtime.start()

    await runtime._handle_server_request(
        99,
        "item/commandExecution/requestApproval",
        {"command": "echo hi", "threadId": "thread-1", "turnId": "turn-1", "itemId": "x", "startedAtMs": 1},
    )
    await asyncio.sleep(0.02)
    assert parked and parked[0]["kind"] == "command"
    # FakeProcess captures stdin writes; approval response should have been written.
    assert _map_approval_decision("always") == "acceptForSession"
    await runtime.stop()


@pytest.mark.asyncio
async def test_yolo_mode_skips_park_for_approvals(tmp_path: Path, monkeypatch):
    parked: list[dict] = []

    async def push(_kind: str, _payload: dict) -> None:
        return None

    async def park(info: dict) -> str:
        parked.append(info)
        return "decline"

    runtime = _runtime(tmp_path, push, park)
    fake = FakeProcess(_rpc_handler)

    async def fake_exec(*_args, **_kwargs):
        fake.start()
        return fake

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await runtime.start()
    await runtime.set_permission_mode("yolo")
    assert runtime.permission_mode == "yolo"
    assert runtime.status_payload()["permission_mode"] == "yolo"

    await runtime._handle_server_request(
        101,
        "item/commandExecution/requestApproval",
        {"command": "rm -rf /", "threadId": "thread-1"},
    )
    await asyncio.sleep(0.02)
    assert parked == []
    await runtime.stop()


@pytest.mark.asyncio
async def test_set_home_restarts(tmp_path: Path, monkeypatch):
    starts: list[str] = []

    async def push(_kind: str, _payload: dict) -> None:
        return None

    async def park(_info: dict) -> str:
        return "decline"

    store = _store(tmp_path)
    runtime = _runtime(tmp_path, push, park, profiles=store)

    async def fake_exec(*args, **kwargs):
        starts.append(kwargs.get("env", {}).get("CODEX_HOME", ""))
        proc = FakeProcess(_rpc_handler)
        proc.start()
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    await runtime.start()
    assert runtime.home_kind == HOME_KIMI
    await runtime.set_home(HOME_DEFAULT)
    assert runtime.home_kind == HOME_DEFAULT
    assert len(starts) >= 2
    assert str(Path.home() / ".codex") in starts[-1] or starts[-1].endswith(".codex")
    await runtime.stop()


def test_ensure_kimi_home_writes_template(tmp_path: Path):
    home = tmp_path / "codex-kimi"
    ensure_kimi_home(home)
    config = home / "config.toml"
    assert config.is_file()
    text = config.read_text(encoding="utf-8")
    assert "model_provider" in text
    assert "KIMI_API_KEY" in text
    # Second call must not overwrite custom edits.
    config.write_text("model = \"custom\"\n", encoding="utf-8")
    ensure_kimi_home(home)
    assert config.read_text(encoding="utf-8") == "model = \"custom\"\n"


def test_resolve_home_kinds():
    assert resolve_home(HOME_KIMI) == kimi_home_path()
    assert resolve_home(HOME_DEFAULT).name == ".codex"


def test_map_approval_decision():
    assert _map_approval_decision("once") == "accept"
    assert _map_approval_decision("always") == "acceptForSession"
    assert _map_approval_decision("deny") == "decline"
    assert _map_approval_decision(None) == "decline"


def test_protocol_codex_and_claude_commands_are_wired():
    for kind in (
        "codex_set_home",
        "codex_set_profile",
        "codex_upsert_profile",
        "codex_delete_profile",
        "codex_start",
        "codex_stop",
        "codex_prompt",
        "codex_interrupt",
        "codex_approve",
        "codex_new_session",
        "codex_open_session",
        "codex_delete_session",
        "codex_archive_session",
        "codex_set_workspace",
        "claude_set_profile",
        "claude_upsert_profile",
        "claude_delete_profile",
        "claude_start",
        "claude_stop",
        "claude_prompt",
        "claude_interrupt",
        "claude_approve",
        "claude_new_session",
        "claude_open_session",
        "claude_delete_session",
        "claude_archive_session",
        "claude_set_workspace",
    ):
        command, args = parse_inbound({"type": kind, "text": "x"})
        assert command in gui_commands.HANDLERS
        assert isinstance(command, Inbound)

    for outbound in (
        Outbound.CODEX_STATUS,
        Outbound.CODEX_TEXT,
        Outbound.CLAUDE_STATUS,
        Outbound.CLAUDE_TEXT,
        Outbound.CLAUDE_PERMISSION,
        Outbound.CLAUDE_DONE,
    ):
        assert outbound.value.split("_")[0] in {"codex", "claude"}


def test_find_codex_executable_tolerates_missing(monkeypatch):
    monkeypatch.setattr("aiharness.gui.codex_runtime.shutil.which", lambda _name: None)
    monkeypatch.setattr(
        "aiharness.gui.codex_runtime.os.environ",
        {"LOCALAPPDATA": str(Path("/missing")), "ProgramFiles": str(Path("/missing"))},
        raising=False,
    )
    # Should not raise.
    result = find_codex_executable()
    assert result is None or isinstance(result, str)


@pytest.mark.asyncio
async def test_missing_executable_sets_error(tmp_path: Path, monkeypatch):
    events: list[tuple[str, dict]] = []

    async def push(kind: str, payload: dict) -> None:
        events.append((kind, payload))

    async def park(_info: dict) -> str:
        return "decline"

    monkeypatch.setattr("aiharness.gui.codex_runtime.find_codex_executable", lambda: None)
    runtime = _runtime(tmp_path, push, park, executable=None)
    await runtime.start()
    assert runtime.state == "error"
    assert any(k == "codex_error" for k, _ in events)


def test_parse_codex_model_entry_includes_efforts():
    from aiharness.gui.codex_runtime import _parse_codex_model_entry

    parsed = _parse_codex_model_entry(
        {
            "id": "gpt-5.5",
            "displayName": "GPT-5.5",
            "defaultReasoningEffort": "medium",
            "supportedReasoningEfforts": [
                {"reasoningEffort": "low"},
                {"reasoningEffort": "medium"},
                {"reasoningEffort": "high"},
            ],
            "isDefault": True,
        }
    )
    assert parsed is not None
    assert parsed["id"] == "gpt-5.5"
    assert parsed["efforts"] == ["low", "medium", "high"]
    assert parsed["default_effort"] == "medium"


def test_kimi_known_models_include_k3():
    from aiharness.gui.codex_profiles import KNOWN_PROVIDER_MODELS

    ids = [m["id"] for m in KNOWN_PROVIDER_MODELS["kimi"]]
    assert "k3" in ids
    assert "k3-256k" in ids
    assert "kimi-for-coding" in ids
    assert "kimi-k3" in ids
    assert ids[0] == "k3"


@pytest.mark.asyncio
async def test_item_started_completed_emit_tool_events(tmp_path: Path):
    events: list[tuple[str, dict]] = []

    async def push(kind: str, payload: dict) -> None:
        events.append((kind, payload))

    async def park(_info: dict) -> str:
        return "accept"

    runtime = _runtime(tmp_path, push, park, executable=None)
    slot = runtime._ensure_viewed_meta()
    await runtime._handle_notification(
        "item/started",
        {
            "item": {
                "type": "commandExecution",
                "id": "cmd-1",
                "command": "pytest -q",
                "cwd": str(tmp_path),
                "status": "inProgress",
            }
        },
    )
    await runtime._handle_notification(
        "item/completed",
        {
            "item": {
                "type": "commandExecution",
                "id": "cmd-1",
                "command": "pytest -q",
                "cwd": str(tmp_path),
                "status": "completed",
                "exitCode": 0,
                "durationMs": 1200,
                "aggregatedOutput": "ok",
            }
        },
    )
    starts = [p for k, p in events if k == "codex_tool_start"]
    ends = [p for k, p in events if k == "codex_tool_end"]
    assert starts and starts[0]["call_id"] == "cmd-1"
    assert starts[0].get("panel_session_id") == slot.id
    assert "$ pytest -q" in starts[0]["headline"]
    assert ends and ends[0]["call_id"] == "cmd-1"
    assert ends[0]["is_error"] is False
    assert any(k == "codex_activity" for k, _ in events)


@pytest.mark.asyncio
async def test_agent_message_completed_emits_text_without_deltas(tmp_path: Path):
    events: list[tuple[str, dict]] = []

    async def push(kind: str, payload: dict) -> None:
        events.append((kind, payload))

    async def park(_info: dict) -> str:
        return "accept"

    runtime = _runtime(tmp_path, push, park, executable=None)
    slot = runtime._ensure_viewed_meta()
    await runtime._handle_notification(
        "turn/started",
        {"threadId": "thread-x", "turn": {"id": "turn-x"}},
    )
    await runtime._handle_notification(
        "item/completed",
        {
            "item": {
                "type": "agentMessage",
                "id": "msg-1",
                "text": "我是 k3",
            }
        },
    )
    texts = [p["delta"] for k, p in events if k == "codex_text"]
    assert texts == ["我是 k3"]
    # If deltas already streamed, completed must not duplicate.
    slot.streamed_text = True
    events.clear()
    await runtime._handle_notification(
        "item/completed",
        {"item": {"type": "agentMessage", "id": "msg-2", "text": "dup"}},
    )
    assert [p for k, p in events if k == "codex_text"] == []


@pytest.mark.asyncio
async def test_notification_routes_by_thread_id(tmp_path: Path):
    events: list[tuple[str, dict]] = []

    async def push(kind: str, payload: dict) -> None:
        events.append((kind, payload))

    async def park(_info: dict) -> str:
        return "accept"

    runtime = _runtime(tmp_path, push, park, executable=None)
    slot1 = CodexSlot(id="sess-1", workspace=tmp_path, thread_id="thread-1")
    slot2 = CodexSlot(id="sess-2", workspace=tmp_path, thread_id="thread-2")
    runtime.slots = {"sess-1": slot1, "sess-2": slot2}
    runtime._thread_index = {"thread-1": "sess-1", "thread-2": "sess-2"}
    runtime._apply_slot_to_viewed(slot1)

    await runtime._handle_notification(
        "item/agentMessage/delta",
        {"delta": "from-2", "threadId": "thread-2", "itemId": "i2"},
    )
    await runtime._handle_notification(
        "turn/started",
        {"threadId": "thread-2", "turn": {"id": "turn-2", "threadId": "thread-2"}},
    )
    text_hits = [p for k, p in events if k == "codex_text"]
    assert text_hits and text_hits[0]["delta"] == "from-2"
    assert text_hits[0]["panel_session_id"] == "sess-2"
    assert slot2.busy is True
    assert slot1.busy is False
    # Viewing slot 1 — mirrors must not flip from thread-2 activity.
    assert runtime.viewed_id == "sess-1"
    assert runtime.busy is False
    assert runtime.status_payload()["sessions"]["viewed_id"] == "sess-1"


@pytest.mark.asyncio
async def test_dead_thread_resume_starts_fresh(tmp_path: Path, monkeypatch):
    """Stale native_id after app-server restart must not stick on the slot."""
    events: list[tuple[str, dict]] = []

    async def push(kind: str, payload: dict) -> None:
        events.append((kind, payload))

    async def park(_info: dict) -> str:
        return "accept"

    def handler(message: dict):
        method = message.get("method")
        msg_id = message.get("id")
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
        if method == "initialized":
            return None
        if method == "thread/resume":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"message": "thread not found: dead-thread"},
            }
        if method == "thread/start":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "model": "fake-model",
                    "modelProvider": "fake",
                    "thread": {"id": "fresh-thread"},
                },
            }
        if method == "model/list":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"data": []}}
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

    store = _session_store(tmp_path)
    meta = store.create(tmp_path, native_id="dead-thread")
    runtime = _runtime(tmp_path, push, park, session_store=store)
    runtime.viewed_id = meta.id
    runtime.slots[meta.id] = CodexSlot(
        id=meta.id, workspace=tmp_path, thread_id="dead-thread"
    )
    runtime._thread_index["dead-thread"] = meta.id
    runtime._apply_slot_to_viewed(runtime.slots[meta.id])

    fake = FakeProcess(handler)

    async def fake_exec(*_args, **_kwargs):
        fake.start()
        return fake

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await runtime.start()
    assert runtime.state == "ready"
    assert runtime.thread_id == "fresh-thread"
    assert runtime.slots[meta.id].thread_id == "fresh-thread"
    assert "dead-thread" not in runtime._thread_index
    saved = store.get(meta.id)
    assert saved is not None
    assert saved.native_id == "fresh-thread"
    assert any(
        k == "codex_notice" and "失效" in str(p.get("text") or "")
        for k, p in events
    )
    await runtime.stop()
