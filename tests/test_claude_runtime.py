"""Tests for the Claude Code panel runtime."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from aiharness.credentials import CredentialStore
from aiharness.gui.claude_profiles import ClaudeProfileStore
from aiharness.gui.claude_runtime import ClaudeRuntime, _extract_text, find_claude_executable
from aiharness.gui.panel_sessions import PanelSessionStore


def _store(tmp_path: Path) -> ClaudeProfileStore:
    return ClaudeProfileStore(
        path=tmp_path / "claude_profiles.json",
        credentials=CredentialStore(path=tmp_path / "creds.json"),
    )


def _session_store(tmp_path: Path) -> PanelSessionStore:
    return PanelSessionStore("claude", root=tmp_path / "claude_panel_sessions")


def _ready_runtime(tmp_path: Path, push, park, profiles=None) -> ClaudeRuntime:
    profiles = profiles or _store(tmp_path)
    profiles.upsert(
        profile_id="anthropic",
        name="Anthropic",
        template="anthropic",
        api_key="sk-ant-test-key-1234567890",
        make_active=True,
    )
    return ClaudeRuntime(
        workspace=tmp_path,
        push=push,
        park_approval=park,
        executable="claude-fake",
        profiles=profiles,
        session_store=_session_store(tmp_path),
    )


class FakeProcess:
    _next_pid = 9000

    def __init__(self):
        self.stdin = _Pipe()
        self.stdout = _Pipe()
        self.stderr = _Pipe()
        self.returncode = None
        FakeProcess._next_pid += 1
        self.pid = FakeProcess._next_pid

    def kill(self):
        self.returncode = -1

    async def wait(self):
        self.returncode = self.returncode if self.returncode is not None else 0


class _Pipe:
    def __init__(self):
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    def write(self, data: bytes) -> None:
        for line in data.decode("utf-8").splitlines(keepends=True):
            if line.endswith("\n"):
                self._queue.put_nowait(line.encode("utf-8"))

    async def drain(self) -> None:
        return None

    def close(self) -> None:
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


@pytest.mark.asyncio
async def test_claude_prompt_streams_text(tmp_path: Path, monkeypatch):
    events: list[tuple[str, dict]] = []

    async def push(kind: str, payload: dict) -> None:
        events.append((kind, payload))

    async def park(_info: dict) -> str:
        return "accept"

    runtime = _ready_runtime(tmp_path, push, park)
    fake = FakeProcess()

    async def fake_exec(*_a, **_k):
        return fake

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await runtime.start()
    assert runtime.state == "ready"
    assert runtime.viewed_id

    await runtime.prompt("hi")
    # Simulate Claude Code stream + result.
    await fake.stdout._queue.put(
        (json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
            "session_id": "s1",
        }) + "\n").encode("utf-8")
    )
    await fake.stdout._queue.put(
        (json.dumps({"type": "result", "result": "hello", "session_id": "s1"}) + "\n").encode("utf-8")
    )
    await asyncio.sleep(0.05)
    texts = "".join(p.get("delta", "") for k, p in events if k == "claude_text")
    assert "hello" in texts
    assert any(k == "claude_done" for k, _ in events)
    text_events = [p for k, p in events if k == "claude_text"]
    assert text_events and all(p.get("panel_session_id") == runtime.viewed_id for p in text_events)
    await runtime.stop()
    assert runtime.state == "stopped"


def test_extract_text_from_assistant_blocks():
    msg = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "abc"}, {"type": "text", "text": "def"}]},
    }
    assert _extract_text(msg) == "abcdef"


def test_extract_text_ignores_partial_json():
    msg = {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "delta": {"type": "input_json_delta", "partial_json": "{\"cmd\":"},
        },
    }
    assert _extract_text(msg) == ""


@pytest.mark.asyncio
async def test_stream_tool_use_emits_tool_events(tmp_path: Path, monkeypatch):
    events: list[tuple[str, dict]] = []

    async def push(kind: str, payload: dict) -> None:
        events.append((kind, payload))

    async def park(_info: dict) -> str:
        return "accept"

    runtime = _ready_runtime(tmp_path, push, park)
    fake = FakeProcess()

    async def fake_exec(*_a, **_k):
        return fake

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await runtime.start()
    await runtime._dispatch({
        "type": "stream_event",
        "event": {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}},
        },
    })
    await runtime._dispatch({
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "done"}],
        },
    })
    assert any(k == "claude_tool_start" and p.get("call_id") == "t1" for k, p in events)
    assert any(k == "claude_tool_end" and p.get("call_id") == "t1" for k, p in events)
    await runtime.stop()


def test_find_claude_tolerates_missing(monkeypatch):
    monkeypatch.setattr("aiharness.gui.claude_runtime.shutil.which", lambda _n: None)
    monkeypatch.setattr(
        "aiharness.gui.claude_runtime.os.environ",
        {"APPDATA": str(Path("/missing")), "LOCALAPPDATA": str(Path("/missing"))},
        raising=False,
    )
    assert find_claude_executable() is None or isinstance(find_claude_executable(), str)


@pytest.mark.asyncio
async def test_missing_claude_sets_error(tmp_path: Path, monkeypatch):
    events: list[tuple[str, dict]] = []

    async def push(kind: str, payload: dict) -> None:
        events.append((kind, payload))

    async def park(_info: dict) -> str:
        return "deny"

    monkeypatch.setattr("aiharness.gui.claude_runtime.find_claude_executable", lambda: None)
    runtime = ClaudeRuntime(
        workspace=tmp_path,
        push=push,
        park_approval=park,
        profiles=_store(tmp_path),
        session_store=_session_store(tmp_path),
    )
    await runtime.start()
    assert runtime.state == "error"
    assert any(k == "claude_error" for k, _ in events)


def test_build_user_content_includes_images():
    from aiharness.gui.claude_runtime import _build_user_content

    content = _build_user_content(
        "see this",
        [{"data_url": "data:image/png;base64,aaaa", "mime": "image/png"}],
    )
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image"


@pytest.mark.asyncio
async def test_start_passes_resume_when_native_id_set(tmp_path: Path, monkeypatch):
    events: list[tuple[str, dict]] = []

    async def push(kind: str, payload: dict) -> None:
        events.append((kind, payload))

    async def park(_info: dict) -> str:
        return "accept"

    from aiharness.gui.claude_runtime import ClaudeSlot

    runtime = _ready_runtime(tmp_path, push, park)
    meta = runtime.store.create(tmp_path, native_id="native-sess-99")
    slot = ClaudeSlot(id=meta.id, workspace=tmp_path, session_id="native-sess-99")
    runtime.slots[meta.id] = slot
    runtime._apply_slot_to_viewed(slot)

    captured: list[tuple] = []

    async def fake_exec(*args, **_k):
        captured.append(args)
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await runtime.start(resume="native-sess-99")
    assert captured
    assert "--resume" in captured[0]
    assert "native-sess-99" in captured[0]
    await runtime.stop()


@pytest.mark.asyncio
async def test_interrupt_preserves_session_id_in_store(tmp_path: Path, monkeypatch):
    events: list[tuple[str, dict]] = []

    async def push(kind: str, payload: dict) -> None:
        events.append((kind, payload))

    async def park(_info: dict) -> str:
        return "accept"

    runtime = _ready_runtime(tmp_path, push, park)
    fakes: list[FakeProcess] = []

    async def fake_exec(*_a, **_k):
        proc = FakeProcess()
        fakes.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await runtime.start()
    slot = runtime._viewed_slot()
    assert slot is not None
    slot.session_id = "keep-me"
    runtime.session_id = "keep-me"
    runtime.store.touch(slot.id, native_id="keep-me")

    await runtime.interrupt()
    meta = runtime.store.get(slot.id)
    assert meta is not None
    assert meta.native_id == "keep-me"
    assert runtime.session_id == "keep-me" or (
        runtime._viewed_slot() and runtime._viewed_slot().session_id == "keep-me"
    )
    # Second process should have resumed.
    assert len(fakes) >= 2
    await runtime.stop()


@pytest.mark.asyncio
async def test_start_includes_stdio_permission_prompt_tool(tmp_path: Path, monkeypatch):
    async def push(_k: str, _p: dict) -> None:
        return None

    async def park(_info: dict) -> str:
        return "accept"

    runtime = _ready_runtime(tmp_path, push, park)
    captured: list[tuple] = []

    async def fake_exec(*args, **_k):
        captured.append(args)
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await runtime.start()
    assert captured
    args = captured[0]
    assert "--permission-prompt-tool" in args
    idx = args.index("--permission-prompt-tool")
    assert args[idx + 1] == "stdio"
    await runtime.stop()


@pytest.mark.asyncio
async def test_control_allow_includes_updated_input(tmp_path: Path, monkeypatch):
    events: list[tuple[str, dict]] = []

    async def push(kind: str, payload: dict) -> None:
        events.append((kind, payload))

    async def park(_info: dict) -> str:
        return "accept"

    runtime = _ready_runtime(tmp_path, push, park)
    fake = FakeProcess()

    async def fake_exec(*_a, **_k):
        return fake

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await runtime.start()
    runtime.permission_mode = "yolo"
    slot = runtime._viewed_slot()
    assert slot is not None
    await runtime._handle_control(
        {
            "type": "control_request",
            "request_id": "req1",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "Bash",
                "input": {"command": "ls", "description": "list"},
            },
        },
        slot=slot,
    )
    # FakeProcess mirrors stdin writes into stdout queue — drain written line.
    written = await fake.stdin.readline()
    payload = json.loads(written.decode("utf-8"))
    assert payload["type"] == "control_response"
    decision = payload["response"]["response"]
    assert decision["behavior"] == "allow"
    assert decision["updatedInput"]["command"] == "ls"
    await runtime.stop()


@pytest.mark.asyncio
async def test_system_status_clears_activity_when_idle(tmp_path: Path, monkeypatch):
    events: list[tuple[str, dict]] = []

    async def push(kind: str, payload: dict) -> None:
        events.append((kind, payload))

    async def park(_info: dict) -> str:
        return "accept"

    runtime = _ready_runtime(tmp_path, push, park)
    fake = FakeProcess()

    async def fake_exec(*_a, **_k):
        return fake

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await runtime.start()
    slot = runtime._viewed_slot()
    assert slot is not None
    slot.busy = False
    await runtime._dispatch({"type": "system", "subtype": "status"}, slot_id=slot.id)
    clears = [p for k, p in events if k == "claude_activity" and p.get("kind") == "clear"]
    assert clears
    stuck = [
        p for k, p in events
        if k == "claude_activity" and "系统" in str(p.get("text") or "")
    ]
    assert not stuck
    await runtime.stop()
