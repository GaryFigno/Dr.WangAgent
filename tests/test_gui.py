"""The desktop GUI: protocol, server, auth and command handling."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
from aiohttp import ClientSession, WSServerHandshakeError

from aiharness.config.schema import Config
from aiharness.credentials import CredentialStore
from aiharness.gui import commands as gui_commands
from aiharness.gui.bridge import GuiSession, tool_headline
from aiharness.gui.protocol import (
    PROTOCOL_VERSION,
    Inbound,
    Outbound,
    ProtocolError,
    message,
    parse_inbound,
)
from aiharness.gui.server import GuiServer

WEB_ROOT = Path(__file__).resolve().parents[1] / "aiharness" / "gui" / "web"


# -- protocol --------------------------------------------------------------


def test_every_inbound_command_has_a_handler():
    """A command the frontend can send but the backend ignores is a dead button."""
    missing = [c.value for c in Inbound if c not in gui_commands.HANDLERS]
    assert missing == []


def test_messages_carry_their_type():
    payload = message(Outbound.NOTICE, level="warn", text="careful")
    assert payload == {"type": "notice", "level": "warn", "text": "careful"}


def test_inbound_parsing_accepts_a_known_command():
    command, args = parse_inbound({"type": "prompt", "text": "hello"})
    assert command is Inbound.PROMPT
    assert args == {"text": "hello"}


@pytest.mark.parametrize(
    "raw", ["not an object", {"no": "type"}, {"type": 7}, {"type": "invented"}]
)
def test_inbound_parsing_rejects_junk(raw):
    with pytest.raises(ProtocolError):
        parse_inbound(raw)


def test_tool_headline_prefers_the_informative_argument():
    assert tool_headline("Bash", {"command": "git status"}) == "Bash(git status)"
    assert tool_headline("Read", {"file_path": "a.py"}) == "Read(a.py)"
    assert tool_headline("Team", {}) == "Team()"


def test_tool_headline_clips_a_long_argument():
    headline = tool_headline("Bash", {"command": "x" * 400})
    assert len(headline) < 120
    assert headline.endswith("…)")


# -- the frontend bundle ---------------------------------------------------


def test_the_frontend_files_are_present():
    for name in ("index.html", "app.css", "app.js", "i18n.js"):
        assert (WEB_ROOT / name).is_file(), name


def test_frontend_loads_i18n_before_app():
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    assert 'src="/static/i18n.js"' in html
    assert html.index("i18n.js") < html.index("app.js")
    assert 'id="language-select"' in html
    assert "data-i18n=" in html


def test_the_frontend_agrees_on_the_protocol_version():
    """A silent version drift shows up as a blank window, not an error."""
    source = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
    match = re.search(r"const PROTOCOL_VERSION = (\d+)", source)
    assert match is not None
    assert int(match.group(1)) == PROTOCOL_VERSION


def test_the_frontend_handles_every_outbound_message():
    """An unhandled event type means a feature that silently does nothing."""
    source = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
    handlers = re.search(r"const HANDLERS = \{(.*?)\n\};", source, re.DOTALL)
    assert handlers is not None
    body = handlers.group(1)
    for kind in Outbound:
        assert re.search(rf"\b{kind.value}\s*\(", body), f"frontend ignores '{kind.value}'"


def test_the_frontend_escapes_model_output():
    """Tool results contain arbitrary text; treating it as markup is a hole."""
    source = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
    assert "function escapeHtml" in source
    # Markdown rendering must escape before it introduces any tags.
    render = source[source.index("function renderMarkdown") :]
    render = render[: render.index("\n}")]
    assert render.index("escapeHtml") < render.index("<pre>")


def test_streaming_text_is_not_parsed_as_markup_per_delta():
    """Per-delta markdown parsing is what makes a chat UI feel heavy."""
    source = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
    append = source[source.index("function appendText") :]
    append = append[: append.index("\n}")]
    assert "textContent" in append
    assert "innerHTML" not in append


# -- the server ------------------------------------------------------------


@pytest.fixture
async def server(workspace):
    instance = GuiServer(Config(), workspace)
    await instance.start()
    yield instance
    await instance.stop()


async def test_health_reports_the_protocol_version(server):
    async with ClientSession() as client:
        async with client.get(f"http://127.0.0.1:{server.port}/health") as response:
            assert response.status == 200
            assert (await response.json())["protocol"] == PROTOCOL_VERSION


async def test_the_frontend_is_served(server):
    async with ClientSession() as client:
        async with client.get(f"http://127.0.0.1:{server.port}/") as response:
            body = await response.text()
        assert response.status == 200
        assert "Dr.Wang" in body
        async with client.get(f"http://127.0.0.1:{server.port}/static/app.js") as script:
            assert script.status == 200


async def test_the_socket_binds_to_loopback_only(server):
    """This socket can run shell commands. It must not be reachable off-box."""
    from aiharness.gui.server import HOST

    assert HOST == "127.0.0.1"
    assert "127.0.0.1" in server.url


async def test_a_wrong_token_is_refused(server):
    async with ClientSession() as client:
        with pytest.raises(WSServerHandshakeError):
            async with client.ws_connect(
                f"ws://127.0.0.1:{server.port}/ws?token=wrong"
            ):
                pass


async def test_a_missing_token_is_refused(server):
    async with ClientSession() as client:
        with pytest.raises(WSServerHandshakeError):
            async with client.ws_connect(f"ws://127.0.0.1:{server.port}/ws"):
                pass


async def test_the_handshake_pushes_the_initial_state(server):
    async with ClientSession() as client:
        async with client.ws_connect(
            f"ws://127.0.0.1:{server.port}/ws?token={server.token}"
        ) as socket:
            kinds = await drain_handshake(socket)
    # Order matters: "ready" first so the frontend can check the protocol
    # version before it tries to render anything.
    assert kinds[0] == "ready"
    assert {"status", "sessions", "config", "workspace"} <= set(kinds)


async def test_malformed_json_gets_an_error_not_a_crash(server):
    async with ClientSession() as client:
        async with client.ws_connect(
            f"ws://127.0.0.1:{server.port}/ws?token={server.token}"
        ) as socket:
            await drain_handshake(socket)
            await socket.send_str("{not json")
            reply = await asyncio.wait_for(socket.receive_json(), timeout=5)
    assert reply["type"] == "error"


async def test_an_unknown_command_gets_an_error(server):
    async with ClientSession() as client:
        async with client.ws_connect(
            f"ws://127.0.0.1:{server.port}/ws?token={server.token}"
        ) as socket:
            await drain_handshake(socket)
            await socket.send_str(json.dumps({"type": "launch_missiles"}))
            reply = await asyncio.wait_for(socket.receive_json(), timeout=5)
    assert reply["type"] == "error"
    assert "unknown command" in reply["message"]


HANDSHAKE_QUIET_SECONDS = 0.4


async def drain_handshake(socket) -> list[str]:
    """Read the opening burst until it stops.

    Deliberately not a fixed count: the handshake gains messages as the app
    grows, and a test that counts them fails for reasons unrelated to what it
    is checking.
    """
    kinds: list[str] = []
    while True:
        try:
            payload = await asyncio.wait_for(
                socket.receive_json(), timeout=HANDSHAKE_QUIET_SECONDS
            )
        except (TimeoutError, asyncio.TimeoutError):
            return kinds
        kinds.append(payload["type"])


# -- the session bridge ----------------------------------------------------


@pytest.fixture
def sent():
    return []


@pytest.fixture
def gui(config, workspace, sent):
    async def send(payload):
        sent.append(payload)

    session = GuiSession(config, workspace, send)
    yield session


def kinds(sent):
    return [item["type"] for item in sent]


def last(sent, kind):
    return next(item for item in reversed(sent) if item["type"] == kind)


async def test_status_reports_the_active_model(gui, sent):
    await gui.push_status()
    status = last(sent, "status")["status"]
    assert status["model"] == "fake"
    assert status["mode"] == "yolo"
    assert status["context_window"] > 0
    await gui.close()


async def test_config_lists_accounts_models_and_roles(gui, sent):
    await gui.push_config()
    config = last(sent, "config")["config"]
    assert [a["id"] for a in config["accounts"]] == ["primary"]
    assert [m["id"] for m in config["models"]] == ["fake"]
    assert any(role["role"] == "main" for role in config["roles"])
    assert config["ready"] is True
    await gui.close()


async def test_config_never_leaks_the_key(gui, sent):
    await gui.push_config()
    config = last(sent, "config")["config"]
    rendered = json.dumps(config)
    assert "key-primary" not in rendered
    assert "..." in config["accounts"][0]["key"]
    await gui.close()


async def test_context_breakdown_is_pushed(gui, sent):
    await gui.push_context()
    payload = last(sent, "context")
    assert payload["window"] > 0
    assert any(row["name"] == "Free space" for row in payload["rows"])
    await gui.close()


async def test_a_parked_question_resolves_on_the_frontend_reply(gui, sent):
    task = asyncio.create_task(gui._park(Outbound.ASK, questions=[]))
    await asyncio.sleep(0.05)
    key = last(sent, "ask")["id"]
    gui.resolve(key, {"Database": "Postgres"})
    assert await asyncio.wait_for(task, timeout=2) == {"Database": "Postgres"}
    await gui.close()


async def test_disconnecting_cancels_parked_questions(gui, sent):
    """Otherwise the agent waits forever for a browser that has gone."""
    task = asyncio.create_task(gui._park(Outbound.PERMISSION, tool="Bash"))
    await asyncio.sleep(0.05)
    gui.cancel_pending()
    assert await asyncio.wait_for(task, timeout=2) is None
    await gui.close()


async def test_permission_always_adds_a_session_rule(gui, sent):
    from aiharness.permissions import Decision, Verdict

    verdict = Verdict(Decision.ASK, "needs approval", suggested_rule="Bash(git diff:*)")
    task = asyncio.create_task(gui._ask_permission("Bash", {"command": "git diff"}, verdict))
    await asyncio.sleep(0.05)
    gui.resolve(last(sent, "permission")["id"], "always")

    assert await asyncio.wait_for(task, timeout=2) is True
    assert "Bash(git diff:*)" in gui.permissions.session_rules()
    await gui.close()


async def test_a_denied_permission_returns_false(gui, sent):
    from aiharness.permissions import Decision, Verdict

    task = asyncio.create_task(
        gui._ask_permission("Bash", {"command": "rm x"}, Verdict(Decision.ASK, "risky"))
    )
    await asyncio.sleep(0.05)
    gui.resolve(last(sent, "permission")["id"], "deny")
    assert await asyncio.wait_for(task, timeout=2) is False
    await gui.close()


# -- command handlers ------------------------------------------------------


async def test_a_prompt_streams_through_to_the_frontend(gui, sent, fake):
    from .fake_openai import Reply

    fake.push(Reply(text="hello from the model"))
    await gui_commands.run_turn(gui, "say hi")

    assert "turn_start" in kinds(sent)
    assert "".join(m["delta"] for m in sent if m["type"] == "text") == "hello from the model"
    assert last(sent, "done")["text"] == "hello from the model"
    await gui.close()


async def test_tool_activity_is_reported_with_a_headline(gui, sent, fake, workspace):
    from .fake_openai import Reply, tool_call

    fake.push(
        Reply(tool_calls=[tool_call("Read", {"file_path": "hello.txt"})]),
        Reply(text="read it"),
    )
    await gui_commands.run_turn(gui, "read hello.txt")

    start = last(sent, "tool_start")
    end = last(sent, "tool_end")
    assert start["name"] == "Read"
    assert "hello.txt" in start["headline"]
    assert end["is_error"] is False
    assert "line one" in end["content"]
    await gui.close()


async def test_setting_an_unknown_model_is_an_error_not_a_crash(gui, sent):
    await gui_commands.dispatch(gui, Inbound.SET_MODEL, {"spec": "ghost"})
    assert last(sent, "error")["message"]
    await gui.close()


async def test_setting_an_invalid_mode_is_refused(gui, sent):
    await gui_commands.dispatch(gui, Inbound.SET_MODE, {"mode": "reckless"})
    assert "unknown mode" in last(sent, "error")["message"]
    assert gui.permissions.mode == "yolo"
    await gui.close()


async def test_capture_screen_command_pushes_screenshot(gui, sent, monkeypatch):
    from aiharness.gui.capture import ScreenCapture

    async def fake_capture(*, hide_self=True, interactive=True):
        assert hide_self is True
        assert interactive is True
        return ScreenCapture(
            mime="image/png",
            data=b"\x89PNG\r\n\x1a\n",
            name="screenshot-test.png",
            width=10,
            height=10,
        )

    monkeypatch.setattr("aiharness.gui.capture.capture_screen", fake_capture)
    await gui_commands.dispatch(gui, Inbound.CAPTURE_SCREEN, {"hide_self": True})
    payload = last(sent, "screenshot")
    assert payload["name"] == "screenshot-test.png"
    assert payload["data"]
    assert payload["open_editor"] is True
    await gui.close()


async def test_capture_screen_reports_failure(gui, sent, monkeypatch):
    from aiharness.gui.capture import CaptureError

    async def boom(*, hide_self=True, interactive=True):
        raise CaptureError("无法截屏：测试")

    monkeypatch.setattr("aiharness.gui.capture.capture_screen", boom)
    await gui_commands.dispatch(gui, Inbound.CAPTURE_SCREEN, {})
    assert "无法截屏" in last(sent, "notice")["text"]
    await gui.close()


async def test_capture_screen_cancelled(gui, sent, monkeypatch):
    from aiharness.gui.capture import CaptureCancelledError

    async def cancel(*, hide_self=True, interactive=True):
        raise CaptureCancelledError("已取消截屏")

    monkeypatch.setattr("aiharness.gui.capture.capture_screen", cancel)
    await gui_commands.dispatch(gui, Inbound.CAPTURE_SCREEN, {})
    payload = last(sent, "screenshot")
    assert payload.get("cancelled") is True
    await gui.close()


async def test_gui_edit_decision_reject(gui, sent, workspace):
    path = workspace / "tracked.txt"
    path.write_text("new\n", encoding="utf-8")
    item = gui.edit_review.add(
        path=path,
        rel="tracked.txt",
        kind="write",
        before="old\n",
        after="new\n",
        created=False,
    )
    await gui_commands.dispatch(
        gui, Inbound.EDIT_DECISION, {"action": "reject", "id": item.id}
    )
    assert path.read_text(encoding="utf-8") == "old\n"
    assert gui.edit_review.pending() == []
    assert any(m.get("type") == "edit_review" for m in sent)
    await gui.close()


async def test_gui_edit_decision_apply_all(gui, sent, workspace):
    path = workspace / "tracked.txt"
    path.write_text("new\n", encoding="utf-8")
    gui.edit_review.add(
        path=path, rel="tracked.txt", kind="write", before="old\n", after="new\n"
    )
    await gui_commands.dispatch(gui, Inbound.EDIT_DECISION, {"action": "apply_all"})
    assert path.read_text(encoding="utf-8") == "new\n"
    assert gui.edit_review.pending() == []
    await gui.close()


async def test_auto_apply_edits_persists(gui, sent):
    from aiharness.config.loader import default_config_path, load_config

    gui.config.ui.auto_apply_edits = False
    await gui_commands.dispatch(gui, Inbound.SET_AUTO_APPLY_EDITS, {"enabled": True})
    assert gui.config.ui.auto_apply_edits is True
    reloaded = load_config(explicit=default_config_path())
    assert reloaded.ui.auto_apply_edits is True
    await gui.close()


async def test_set_language_persists_in_ui_prefs(gui, sent):
    from aiharness.ui.prefs import UIPrefs

    await gui_commands.dispatch(gui, Inbound.SET_LANGUAGE, {"language": "ja"})
    assert gui.ui_language() == "ja"
    assert UIPrefs.load().language == "ja"
    status = last(sent, "status")["status"]
    assert status["language"] == "ja"
    config = last(sent, "config")["config"]
    assert any(item["code"] == "ja" for item in config.get("languages") or [])
    await gui.close()


async def test_save_canvas_writes_workspace(gui, sent, workspace):
    rel = "docs/canvas.html"
    await gui_commands.dispatch(
        gui,
        Inbound.SAVE_CANVAS,
        {"path": rel, "content": "<html><body>hi</body></html>"},
    )
    path = workspace / rel
    assert path.is_file()
    assert "hi" in path.read_text(encoding="utf-8")
    assert any(m.get("type") == "canvas_hint" for m in sent)
    await gui.close()


async def test_plan_mode_clears_without_sticking(gui, sent):
    await gui_commands.dispatch(gui, Inbound.SET_PLAN_MODE, {"enabled": True})
    assert gui.permissions.plan_mode is True
    await gui_commands.dispatch(gui, Inbound.SET_PLAN_MODE, {"enabled": False})
    assert gui.permissions.plan_mode is False
    assert gui.plan is None
    await gui.close()


async def test_permission_mode_is_remembered_per_session(gui, sent, tmp_path, monkeypatch):
    """Each chat keeps ask/auto/yolo; last choice also becomes the restart default."""
    from aiharness.config.loader import default_config_path, load_config
    from aiharness.providers.base import Message

    assert gui.permissions.mode == "yolo"
    await gui_commands.dispatch(gui, Inbound.SET_MODE, {"mode": "ask"})
    first_id = gui.session.meta.id
    assert gui.session.meta.permission_mode == "ask"
    gui.session.append(Message(role="user", content="keep me"))

    await gui_commands.dispatch(gui, Inbound.NEW_SESSION, {})
    assert gui.permissions.mode == "ask"  # seeded from last choice
    await gui_commands.dispatch(gui, Inbound.SET_MODE, {"mode": "auto"})
    assert gui.session.meta.permission_mode == "auto"

    await gui_commands.dispatch(gui, Inbound.OPEN_SESSION, {"id": first_id})
    assert gui.permissions.mode == "ask"
    assert last(sent, "status")["status"]["mode"] == "ask"

    reloaded = load_config(explicit=default_config_path())
    assert reloaded.permissions.mode == "auto"  # newest choice persisted
    await gui.close()


def _add_second_model(gui):
    """Attach a second selectable model on the same fake account."""
    from aiharness.config.schema import EffortSpec, ModelDef, Pricing

    gui.config.models.append(
        ModelDef(
            id="other",
            model="other-model",
            accounts=["primary"],
            context_windows=[4000],
            default_context=4000,
            max_output_tokens=256,
            effort=EffortSpec(mode="reasoning_effort", levels={"low": "low"}),
            default_effort="low",
            pricing=Pricing(input=1.0, output=2.0, cached_input=0.1),
        )
    )


async def test_open_session_restores_its_own_model(gui, sent):
    """Switching chats must restore that chat's picker, not the previous one."""
    from aiharness.providers.base import Message

    _add_second_model(gui)
    await gui_commands.dispatch(gui, Inbound.SET_MODEL, {"spec": "other@primary"})
    first_id = gui.session.meta.id
    assert gui.session.meta.model == "other"
    # Empty chats are pruned on 「新会话」; keep this one alive.
    gui.session.append(Message(role="user", content="keep me"))

    await gui_commands.dispatch(gui, Inbound.NEW_SESSION, {})
    assert gui.agent.selection.model_id == "fake"
    assert gui.session.meta.id != first_id

    await gui_commands.dispatch(gui, Inbound.OPEN_SESSION, {"id": first_id})
    assert gui.session.meta.id == first_id
    assert gui.agent.selection.model_id == "other"
    assert gui.agent.selection.account_id == "primary"
    status = last(sent, "status")["status"]
    assert status["model"] == "other"
    await gui.close()


async def test_new_session_uses_default_conversation_model(gui, sent):
    """A fresh chat falls back to roles.main, not the previous picker."""
    from aiharness.providers.base import Message

    _add_second_model(gui)
    await gui_commands.dispatch(gui, Inbound.SET_MODEL, {"spec": "other@primary"})
    gui.session.append(Message(role="user", content="keep me"))
    await gui_commands.dispatch(gui, Inbound.NEW_SESSION, {})
    assert gui.agent.selection.model_id == "fake"
    assert gui.session.meta.model == "fake"
    await gui.close()


async def test_set_role_main_does_not_hijack_open_session(gui, sent):
    """Config default is for new chats only; the dialog picker owns this one."""
    _add_second_model(gui)
    await gui_commands.dispatch(gui, Inbound.SET_MODEL, {"spec": "other@primary"})
    await gui_commands.dispatch(
        gui, Inbound.SET_ROLE, {"role": "main", "spec": "fake@primary"}
    )
    assert gui.agent.selection.model_id == "other"
    assert gui.config.role("main").model == "fake"
    await gui.close()


async def test_set_model_blocked_while_turn_running(gui, sent):
    """Mid-turn model swaps break DeepSeek thinking; lock until the turn ends."""
    from aiharness.gui.bridge import LiveTurn

    _add_second_model(gui)
    before = gui.agent.selection.model_id
    sid = gui.session.meta.id
    gui.live[sid] = LiveTurn(
        session_id=sid, handle=gui.session, agent=gui.agent, task=None
    )
    await gui_commands.dispatch(gui, Inbound.SET_MODEL, {"spec": "other@primary"})
    assert gui.agent.selection.model_id == before
    notice = last(sent, "notice")
    assert "模型" in notice["text"] or "model" in notice["text"].lower()
    gui.live.pop(sid, None)
    await gui_commands.dispatch(gui, Inbound.SET_MODEL, {"spec": "other@primary"})
    assert gui.agent.selection.model_id == "other"
    await gui.close()


async def test_steer_queues_into_live_agent(gui, sent):
    from aiharness.gui.bridge import LiveTurn

    sid = gui.session.meta.id
    gui.live[sid] = LiveTurn(
        session_id=sid, handle=gui.session, agent=gui.agent, task=None
    )
    await gui_commands.dispatch(gui, Inbound.STEER, {"text": "中途插队"})
    assert gui.agent._take_steering() == ["中途插队"]
    assert "引导" in last(sent, "notice")["text"]
    gui.live.pop(sid, None)
    await gui.close()


async def test_interrupt_uses_activity_not_sticky_notice(gui, sent):
    """「正在打断」must not sit forever in the transcript while tools continue."""
    from aiharness.gui.bridge import LiveTurn

    sid = gui.session.meta.id
    gui.live[sid] = LiveTurn(
        session_id=sid, handle=gui.session, agent=gui.agent, task=None
    )
    await gui_commands.dispatch(gui, Inbound.INTERRUPT, {})
    assert gui.agent._cancel.is_set()
    activity = last(sent, "activity")
    assert "打断" in activity["text"] or "Interrupt" in activity["text"]
    notices = [m for m in sent if m.get("type") == "notice"]
    assert not any("正在打断" in (m.get("text") or "") for m in notices)
    gui.live.pop(sid, None)
    await gui.close()


async def test_refresh_skips_transcript_replay_while_busy(gui, sent):
    """Mid-turn READY wipe made streamed answers vanish until restart."""
    from aiharness.gui.bridge import LiveTurn

    sid = gui.session.meta.id
    gui.live[sid] = LiveTurn(
        session_id=sid, handle=gui.session, agent=gui.agent, task=None
    )
    sent.clear()
    await gui_commands.dispatch(gui, Inbound.REFRESH, {})
    assert not any(m.get("type") == "ready" for m in sent)

    sent.clear()
    await gui_commands.dispatch(gui, Inbound.REFRESH, {"transcript": True})
    assert any(m.get("type") == "ready" for m in sent)

    gui.live.pop(sid, None)
    sent.clear()
    await gui_commands.dispatch(gui, Inbound.REFRESH, {})
    assert any(m.get("type") == "ready" for m in sent)
    await gui.close()


async def test_new_session_clears_sticky_plan_mode(gui, sent):
    """Plan mode lived on the GuiSession, so one project poisoned every chat."""
    gui.permissions.set_plan_mode(True)
    await gui_commands.dispatch(gui, Inbound.NEW_SESSION, {})
    assert gui.permissions.plan_mode is False
    status = last(sent, "status")["status"]
    assert status["plan_mode"] is False
    await gui.close()


async def test_plan_badge_can_exit_plan_mode(gui, sent):
    gui.permissions.set_plan_mode(True)
    await gui_commands.dispatch(gui, Inbound.SET_PLAN_MODE, {"enabled": False})
    assert gui.permissions.plan_mode is False
    assert "退出" in last(sent, "notice")["text"]
    await gui.close()


async def test_auto_classify_toggle_persists(gui, sent, tmp_path, monkeypatch):
    from aiharness.config.loader import default_config_path, load_config

    assert gui.config.planning.auto_classify is False  # conftest default for tests
    gui.config.planning.auto_classify = True
    await gui_commands.dispatch(gui, Inbound.SET_AUTO_CLASSIFY, {"enabled": False})
    assert gui.config.planning.auto_classify is False
    reloaded = load_config(explicit=default_config_path())
    assert reloaded.planning.auto_classify is False
    await gui.close()


async def test_simple_classify_with_questions_still_answers(gui, sent, fake):
    """Routine score + invented questions must not park before the real reply."""
    from .fake_openai import Reply

    gui.config.planning.auto_classify = True
    gui.config.planning.ask_when_unclear = True
    fake.push(
        Reply(
            text=json.dumps(
                {
                    "score": 3,
                    "reason": "写朋友圈文案，目标明确",
                    "questions": [
                        {
                            "question": "偏正式还是口语？",
                            "header": "语气",
                            "options": [
                                {"label": "正式", "description": "偏公文"},
                                {"label": "口语", "description": "朋友圈风"},
                            ],
                        }
                    ],
                }
            )
        ),
        Reply(text="【朋友圈文案】一键切换 Codex / Claude，CLI 看得见。"),
    )
    await gui_commands.dispatch(
        gui,
        Inbound.PROMPT,
        {"text": "我要怎么在朋友圈里面宣传我们的软件？"},
    )
    assert gui._turn_task is not None
    await gui._turn_task
    assert not any(m.get("type") == "ask" for m in sent), sent
    notices = [m.get("text", "") for m in sent if m.get("type") == "notice"]
    assert any("常规任务" in text and "3/10" in text for text in notices)
    texts = [
        m.get("delta") or m.get("text") or ""
        for m in sent
        if m.get("type") in {"text", "done"}
    ]
    assert any("朋友圈文案" in text for text in texts)
    await gui.close()


async def test_set_draft_is_returned_on_status(gui, sent, tmp_path):
    from aiharness.gui.drafts import DraftStore

    gui.drafts = DraftStore(tmp_path / "drafts.json")
    await gui_commands.dispatch(gui, Inbound.SET_DRAFT, {"text": "未发送的内容"})
    await gui.push_status()
    draft = next(
        item["status"]["draft"]
        for item in reversed(sent)
        if item.get("type") == "status"
    )
    assert draft == "未发送的内容"
    await gui.close()


async def test_sending_a_prompt_clears_the_draft(gui, sent, tmp_path, fake):
    from aiharness.gui.drafts import DraftStore

    from .fake_openai import Reply

    gui.drafts = DraftStore(tmp_path / "drafts.json")
    session_id = gui.session.meta.id
    gui.drafts.set(session_id, "should vanish")
    fake.push(Reply(text="ok"))
    await gui_commands.dispatch(gui, Inbound.PROMPT, {"text": "hello"})
    assert gui.drafts.get(session_id) == ""
    if gui._turn_task is not None:
        await gui._turn_task
    await gui.close()


async def test_deleting_a_session_drops_its_draft(gui, sent, tmp_path):
    from aiharness.gui.drafts import DraftStore

    gui.drafts = DraftStore(tmp_path / "drafts.json")
    session_id = gui.session.meta.id
    gui.drafts.set(session_id, "orphan risk")
    await gui_commands.dispatch(gui, Inbound.DELETE_SESSION, {"id": session_id})
    assert gui.drafts.get(session_id) == ""
    await gui.close()


async def test_tool_end_forwards_display_path(gui, sent, fake, workspace):
    from .fake_openai import Reply, tool_call

    target = workspace / "notes.txt"
    target.write_text("hello", encoding="utf-8")
    fake.push(
        Reply(tool_calls=[tool_call("Read", {"file_path": "notes.txt"})]),
        Reply(text="done"),
    )
    await gui_commands.dispatch(gui, Inbound.PROMPT, {"text": "read notes"})
    if gui._turn_task is not None:
        await gui._turn_task
    ends = [item for item in sent if item.get("type") == "tool_end"]
    assert ends
    assert ends[0].get("display", {}).get("path")
    assert Path(ends[0]["display"]["path"]).name == "notes.txt"
    await gui.close()


async def test_open_path_rejects_outside_workspace(gui, sent, tmp_path):
    # gui.workspace is tmp_path itself — pick a sibling directory.
    outside = tmp_path.parent / f"outside-{tmp_path.name}" / "secret.txt"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("nope", encoding="utf-8")
    await gui_commands.dispatch(
        gui, Inbound.OPEN_PATH, {"path": str(outside), "mode": "reveal"}
    )
    notices = [item for item in sent if item.get("type") == "notice"]
    assert any("workspace" in item.get("text", "").lower() or "工作区" in item.get("text", "") for item in notices)
    await gui.close()


async def test_prompt_with_images_persists_attachments(gui, sent, tmp_path, fake):
    import base64
    from io import BytesIO

    from PIL import Image

    from .fake_openai import Reply

    buffer = BytesIO()
    Image.new("RGB", (4, 4), "green").save(buffer, format="PNG")
    payload = base64.b64encode(buffer.getvalue()).decode()
    fake.push(Reply(text="saw it"))
    await gui_commands.dispatch(
        gui,
        Inbound.PROMPT,
        {
            "text": "look",
            "images": [{"mime": "image/png", "data": payload, "name": "g.png"}],
        },
    )
    if gui._turn_task is not None:
        await gui._turn_task
    user = gui.agent.messages[0]
    assert user.meta.get("attachments")
    path = gui.session.directory / user.meta["attachments"][0]["file"]
    assert path.is_file()
    await gui.push_transcript()
    ready = next(item for item in reversed(sent) if item.get("type") == "ready")
    assert ready["transcript"][0]["images"]
    await gui.close()


async def test_a_role_pointing_at_an_unconfigured_model_is_refused(gui, sent):
    await gui_commands.dispatch(gui, Inbound.SET_ROLE, {"role": "main", "spec": "invented"})
    assert "no model" in last(sent, "error")["message"]
    await gui.close()


def account_result(sent):
    """The inline verdict rendered under the account form."""
    return last(sent, "config")["account_result"]


async def test_adding_an_account_needs_the_env_var_to_exist(gui, sent):
    await gui_commands.dispatch(
        gui,
        Inbound.ADD_ACCOUNT,
        {"id": "new", "base_url": "https://x.example.com/v1", "env": "NOT_SET_HERE"},
    )
    verdict = account_result(sent)
    assert verdict["ok"] is False
    assert "NOT_SET_HERE" in verdict["text"]
    assert gui.config.account("new") is None
    await gui.close()


async def test_a_pasted_key_is_accepted_and_kept_out_of_the_config(
    gui, sent, fake, tmp_path, monkeypatch
):
    """The key in the clipboard is usable without exporting it first.

    It must land in the credential store, never in the config file — that is
    the whole reason the two forms are distinguished.
    """
    monkeypatch.setenv("AIH_CREDENTIALS_FILE", str(tmp_path / "credentials.json"))
    await gui_commands.dispatch(
        gui,
        Inbound.ADD_ACCOUNT,
        {
            "id": "pasted",
            "base_url": fake.base_url,
            "credential": "sk-abcdef0123456789abcdef",
        },
    )
    account = gui.config.account("pasted")
    assert account is not None
    assert account.api_key_env == ""
    assert CredentialStore().get("pasted") == "sk-abcdef0123456789abcdef"
    # What would be written to disk carries no secret.
    assert "sk-" not in account.for_storage().api_key
    await gui.close()


async def test_adding_an_account_writes_the_config_without_a_second_click(
    gui, sent, fake, tmp_path, monkeypatch
):
    """Closing the app right after "添加" used to lose the account."""
    from aiharness.config.loader import default_config_path, load_config

    monkeypatch.setenv("AIH_CREDENTIALS_FILE", str(tmp_path / "credentials.json"))
    config_path = default_config_path()
    await gui_commands.dispatch(
        gui,
        Inbound.ADD_ACCOUNT,
        {
            "id": "keepme",
            "base_url": fake.base_url,
            "credential": "sk-abcdef0123456789abcdef",
        },
    )
    assert gui.config.account("keepme") is not None
    assert any(
        msg.get("type") == "notice" and "已保存" in msg.get("text", "")
        for msg in sent
    )
    reloaded = load_config(explicit=config_path)
    assert reloaded.account("keepme") is not None
    assert "sk-" not in config_path.read_text(encoding="utf-8")
    await gui.close()


async def test_a_credential_that_is_neither_shape_is_refused(gui, sent):
    """Refusing beats guessing: a typo must not become an account."""
    await gui_commands.dispatch(
        gui,
        Inbound.ADD_ACCOUNT,
        {"id": "typo", "base_url": "https://x.example.com/v1", "credential": "my key"},
    )
    assert account_result(sent)["ok"] is False
    assert gui.config.account("typo") is None
    await gui.close()


async def test_adding_an_account_verifies_it_first(gui, sent, fake, monkeypatch):
    monkeypatch.setenv("GUI_TEST_KEY", "sk-value")
    await gui_commands.dispatch(
        gui,
        Inbound.ADD_ACCOUNT,
        {"id": "second", "base_url": fake.base_url, "env": "GUI_TEST_KEY"},
    )
    assert gui.config.account("second") is not None
    assert gui.config.account("second").api_key_env == "GUI_TEST_KEY"
    await gui.close()


async def test_an_unreachable_account_is_not_added(gui, sent, monkeypatch):
    monkeypatch.setenv("GUI_TEST_KEY", "sk-value")
    await gui_commands.dispatch(
        gui,
        Inbound.ADD_ACCOUNT,
        {"id": "dead", "base_url": "http://127.0.0.1:1/v1", "env": "GUI_TEST_KEY"},
    )
    verdict = account_result(sent)
    assert verdict["ok"] is False
    assert "没有保存" in verdict["text"]
    assert gui.config.account("dead") is None
    await gui.close()


async def test_the_model_catalogue_comes_from_the_account(gui, sent):
    await gui_commands.dispatch(gui, Inbound.LIST_ACCOUNT_MODELS, {"id": "primary"})
    catalogue = last(sent, "config")["catalogue"]
    assert catalogue["ok"] is True
    assert any(entry["id"] == "fake-model" for entry in catalogue["models"])
    await gui.close()


async def test_a_model_still_bound_to_a_role_cannot_be_removed(gui, sent):
    await gui_commands.dispatch(gui, Inbound.REMOVE_MODEL, {"id": "fake"})
    assert "still bound" in last(sent, "error")["message"]
    assert gui.config.model("fake") is not None
    await gui.close()


async def test_a_new_session_replaces_the_open_one(gui, sent):
    first = gui.session.meta.id
    await gui_commands.dispatch(gui, Inbound.NEW_SESSION, {})
    assert gui.session.meta.id != first
    assert gui.agent.messages == []
    await gui.close()


async def test_opening_a_missing_session_is_an_error(gui, sent):
    await gui_commands.dispatch(gui, Inbound.OPEN_SESSION, {"id": "nope"})
    assert last(sent, "error")["message"] == "没有这个会话"
    await gui.close()


async def test_a_failing_handler_reports_instead_of_killing_the_socket(gui, sent):
    async def explode(session, args):
        raise RuntimeError("boom")

    gui_commands.HANDLERS[Inbound.REFRESH] = explode
    try:
        await gui_commands.dispatch(gui, Inbound.REFRESH, {})
        assert "boom" in last(sent, "error")["message"]
    finally:
        gui_commands.HANDLERS[Inbound.REFRESH] = gui_commands._refresh
    await gui.close()


async def test_starting_a_heartbeat_arms_it_rather_than_running_it(gui, sent):
    """The dialog sets caps; the composer supplies the goal.

    Nothing may start iterating before a goal exists, so the intermediate
    "armed" state has to be visible rather than implied.
    """
    await gui_commands.dispatch(
        gui, Inbound.START_HEARTBEAT, {"iterations": "3", "interval": 600}
    )
    beat = last(sent, "heartbeat")
    assert beat["armed"] is True
    assert beat["active"] is False
    assert not gui.heartbeat.active
    assert gui.armed_limits is not None

    await gui_commands.dispatch(gui, Inbound.STOP_HEARTBEAT, {})
    assert last(sent, "heartbeat")["armed"] is False
    assert gui.armed_limits is None
    await gui.close()


async def test_the_next_prompt_becomes_the_goal_of_an_armed_heartbeat(gui, sent):
    await gui_commands.dispatch(
        gui, Inbound.START_HEARTBEAT, {"iterations": "3", "interval": 600}
    )
    await gui_commands.dispatch(gui, Inbound.PROMPT, {"text": "把测试全部修好"})
    assert gui.heartbeat.active
    assert gui.heartbeat.state.goal == "把测试全部修好"
    assert gui.armed_limits is None, "arming is one-shot"
    assert last(sent, "heartbeat")["active"] is True

    await gui_commands.dispatch(gui, Inbound.STOP_HEARTBEAT, {})
    await gui.close()


async def test_a_heartbeat_with_every_cap_blank_is_refused(gui, sent):
    """An unbounded agent spending real money is not an option to offer."""
    await gui_commands.dispatch(
        gui, Inbound.START_HEARTBEAT, {"iterations": "", "cost": "", "minutes": ""}
    )
    assert last(sent, "error")["message"]
    assert gui.armed_limits is None
    assert not gui.heartbeat.active
    await gui.close()


# -- workspace -------------------------------------------------------------


async def test_status_reports_the_workspace(gui, sent, workspace):
    await gui.push_status()
    status = last(sent, "status")["status"]
    assert status["workspace"] == str(workspace)
    assert status["workspace_name"] == workspace.name


async def test_changing_workspace_rebuilds_everything_scoped_to_it(gui, sent, tmp_path):
    """Sharing a permission boundary across projects would be a real hazard."""
    other = tmp_path / "another-project"
    other.mkdir()
    first_session = gui.session.meta.id

    await gui_commands.dispatch(gui, Inbound.SET_WORKSPACE, {"path": str(other)})

    assert gui.workspace == other.resolve()
    assert gui.permissions.workspace == other.resolve()
    assert gui.agent.workspace == other.resolve()
    assert gui.session.meta.id != first_session
    await gui.close()


async def test_a_missing_workspace_is_refused(gui, sent, tmp_path):
    await gui_commands.dispatch(
        gui, Inbound.SET_WORKSPACE, {"path": str(tmp_path / "does-not-exist")}
    )
    assert "not a directory" in last(sent, "error")["message"]
    await gui.close()


async def test_a_file_is_not_a_workspace(gui, sent, workspace):
    await gui_commands.dispatch(
        gui, Inbound.SET_WORKSPACE, {"path": str(workspace / "hello.txt")}
    )
    assert "not a directory" in last(sent, "error")["message"]
    await gui.close()


async def test_recent_workspaces_survive_a_reload(tmp_path, monkeypatch):
    from aiharness.gui.workspace import RecentWorkspaces

    path = tmp_path / "workspaces.json"
    monkeypatch.setenv("AIH_WORKSPACES_FILE", str(path))

    recents = RecentWorkspaces.load(path)
    recents.remember(tmp_path / "a")
    recents.remember(tmp_path / "b")
    recents.save(path)

    reloaded = RecentWorkspaces.load(path)
    assert reloaded.paths[0] == str(tmp_path / "b")  # newest first


def test_recents_hide_folders_that_no_longer_exist(tmp_path):
    """A list of dead links is worse than no list."""
    from aiharness.gui.workspace import RecentWorkspaces

    alive = tmp_path / "alive"
    alive.mkdir()
    recents = RecentWorkspaces(paths=[str(alive), str(tmp_path / "deleted")])
    assert recents.existing() == [str(alive)]


def test_recents_are_bounded(tmp_path):
    from aiharness.gui.workspace import MAX_RECENTS, RecentWorkspaces

    recents = RecentWorkspaces()
    for index in range(MAX_RECENTS + 10):
        recents.remember(tmp_path / str(index))
    assert len(recents.paths) == MAX_RECENTS


def test_a_repeated_workspace_moves_to_the_front(tmp_path):
    from aiharness.gui.workspace import RecentWorkspaces

    recents = RecentWorkspaces()
    recents.remember(tmp_path / "a")
    recents.remember(tmp_path / "b")
    recents.remember(tmp_path / "a")
    assert recents.paths == [str(tmp_path / "a"), str(tmp_path / "b")]


def test_the_folder_dialog_is_absent_without_a_window():
    """In browser mode there is no window to host a native dialog."""
    from aiharness.gui import workspace as ws

    ws._holder.window = None
    assert ws.native_folder_dialog() is None


# -- skills ----------------------------------------------------------------


async def test_skills_are_listed_with_their_source(gui, sent, workspace):
    folder = workspace / ".aiharness" / "skills" / "demo"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: demo\ndescription: A demo skill. Use when demonstrating.\n---\n\nbody\n",
        encoding="utf-8",
    )
    await gui_commands.dispatch(gui, Inbound.RELOAD_SKILLS, {})

    payload = last(sent, "skills")
    assert [s["name"] for s in payload["skills"]] == ["demo"]
    assert payload["skills"][0]["source"] == "project"
    assert any("skills" in root for root in payload["roots"])
    await gui.close()


async def test_an_extra_skill_directory_can_be_added(gui, sent, tmp_path):
    shared = tmp_path / "shared-skills" / "thing"
    shared.mkdir(parents=True)
    (shared / "SKILL.md").write_text(
        "---\nname: thing\ndescription: Does the thing. Use for things.\n---\n\nbody\n",
        encoding="utf-8",
    )
    await gui_commands.dispatch(
        gui, Inbound.ADD_SKILL_PATH, {"path": str(tmp_path / "shared-skills")}
    )

    payload = last(sent, "skills")
    assert "thing" in [s["name"] for s in payload["skills"]]
    assert str(tmp_path / "shared-skills") in payload["paths"]
    await gui.close()


async def test_a_skill_directory_that_is_not_a_directory_is_refused(gui, sent, workspace):
    await gui_commands.dispatch(
        gui, Inbound.ADD_SKILL_PATH, {"path": str(workspace / "hello.txt")}
    )
    assert "不是一个目录" in last(sent, "error")["message"]
    await gui.close()


async def test_a_skill_directory_can_be_removed(gui, sent, tmp_path):
    folder = tmp_path / "removable"
    folder.mkdir()
    await gui_commands.dispatch(gui, Inbound.ADD_SKILL_PATH, {"path": str(folder)})
    assert str(folder) in gui.config.skill_paths

    await gui_commands.dispatch(gui, Inbound.REMOVE_SKILL_PATH, {"path": str(folder)})
    assert str(folder) not in gui.config.skill_paths
    await gui.close()


async def test_reloading_skills_invalidates_the_system_prompt(gui, sent):
    """A new skill must reach the model, which means rebuilding the prompt."""
    before = gui.agent.system_prompt()
    folder = gui.workspace / ".aiharness" / "skills" / "fresh"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: fresh\ndescription: Brand new. Use when new.\n---\n\nbody\n",
        encoding="utf-8",
    )
    await gui_commands.dispatch(gui, Inbound.RELOAD_SKILLS, {})

    assert "fresh" in gui.agent.system_prompt()
    assert gui.agent.system_prompt() != before
    await gui.close()


# -- the frontend and backend must agree on the vocabulary -----------------
#
# These caught a real regression: deduplicating the protocol enum quietly
# dropped a command the frontend still sent, and the only symptom was a
# stack of "unknown command" toasts at runtime. Nothing else in the test
# suite crosses the Python/JavaScript boundary, so nothing else could.

WEB_ROOT = Path(__file__).resolve().parents[1] / "aiharness" / "gui" / "web"
SEND_CALL = re.compile(r'send\(\s*"([a-z_]+)"')
#: A handler in the frontend's dispatch table: `name(msg) {` or `name() {`.
HANDLER_KEY = re.compile(r"^\s{2}([a-z_]+)\([a-z]*\)\s*\{", re.MULTILINE)


def frontend_source() -> str:
    return (WEB_ROOT / "app.js").read_text(encoding="utf-8")


def test_every_command_the_frontend_sends_exists_in_the_backend():
    sent = set(SEND_CALL.findall(frontend_source()))
    known = {command.value for command in Inbound}
    assert sent, "no send() calls found — the regex has gone stale"
    assert sent <= known, f"frontend sends unknown commands: {sorted(sent - known)}"


def test_every_backend_command_has_a_handler():
    from aiharness.gui.commands import HANDLERS

    missing = [command.value for command in Inbound if command not in HANDLERS]
    assert not missing, f"commands with no handler: {missing}"


def test_every_event_the_backend_pushes_is_handled_by_the_frontend():
    handled = set(HANDLER_KEY.findall(frontend_source()))
    pushed = {event.value for event in Outbound}
    assert handled, "no frontend handlers found — the regex has gone stale"
    unhandled = pushed - handled
    assert not unhandled, f"frontend ignores backend events: {sorted(unhandled)}"


def test_every_element_the_frontend_touches_exists_in_the_html():
    """A typo in an element id is silently `null` until something breaks."""
    markup = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    referenced = set(re.findall(r'\$\("([a-z0-9-]+)"\)', frontend_source()))
    present = set(re.findall(r'id="([a-z0-9-]+)"', markup))
    assert referenced, "no $() lookups found — the regex has gone stale"
    assert referenced <= present, f"missing from index.html: {sorted(referenced - present)}"


# -- opt-in capabilities ---------------------------------------------------


async def test_desktop_and_browser_start_switched_off(gui, sent):
    """Nothing that reaches outside the workspace is on by default."""
    await gui_commands.dispatch(gui, Inbound.REFRESH, {})
    caps = {c["id"]: c for c in last(sent, "config")["config"]["capabilities"]}
    assert set(caps) == {"desktop", "browser"}
    assert not any(c["enabled"] for c in caps.values())
    assert all(c["detail"] for c in caps.values()), "each needs its reason shown"
    await gui.close()


async def test_enabling_a_capability_adds_its_tools_to_the_registry(gui, sent):
    """The switch rebuilds the tool list rather than filtering at call time.

    While a capability is off the model is not told the tools exist, so it
    cannot ask for what it has not been granted — refusing a call the model
    can see is a weaker guarantee than never offering it.
    """
    before = set(gui.agent.tools.names())
    assert not any(name.startswith("Browser") for name in before)

    await gui_commands.dispatch(gui, Inbound.SET_CAPABILITY, {"id": "browser", "enabled": True})
    assert gui.config.browser.enabled is True
    after = set(gui.agent.tools.names())
    assert after > before

    await gui_commands.dispatch(gui, Inbound.SET_CAPABILITY, {"id": "browser", "enabled": False})
    assert set(gui.agent.tools.names()) == before
    await gui.close()


async def test_an_unknown_capability_is_refused(gui, sent):
    await gui_commands.dispatch(gui, Inbound.SET_CAPABILITY, {"id": "filesystem", "enabled": True})
    assert last(sent, "error")["message"]
    await gui.close()


# -- per-account proxy -----------------------------------------------------


async def test_the_account_list_reports_each_route(gui, sent):
    await gui_commands.dispatch(gui, Inbound.REFRESH, {})
    accounts = last(sent, "config")["config"]["accounts"]
    assert all("proxy_label" in a for a in accounts)
    assert accounts[0]["proxy_label"] == "跟随系统"
    await gui.close()


async def test_changing_one_account_route_leaves_the_others_alone(gui, sent, fake):
    from aiharness.config.schema import ProviderAccount

    gui.config.accounts.append(ProviderAccount(id="second", base_url=fake.base_url))
    await gui_commands.dispatch(
        gui, Inbound.SET_ACCOUNT_PROXY, {"id": "second", "proxy": "direct"}
    )
    assert gui.config.account("second").proxy == "direct"
    assert gui.config.account("primary").proxy == ""
    await gui.close()


async def test_a_nonsense_proxy_is_refused_and_changes_nothing(gui, sent):
    await gui_commands.dispatch(
        gui, Inbound.SET_ACCOUNT_PROXY, {"id": "primary", "proxy": "ftp://x:21"}
    )
    assert last(sent, "error")["message"]
    assert gui.config.account("primary").proxy == ""
    await gui.close()


# -- removing a project ----------------------------------------------------


async def test_forgetting_a_project_updates_both_places_it_is_shown(gui, sent, tmp_path):
    """The sidebar and the composer chips are fed by different messages.

    Only the sidebar was refreshed, so a removed project stayed visible above
    the composer and could still be clicked.
    """
    other = tmp_path / "other-project"
    other.mkdir()
    gui.remember_workspace(other)
    await gui_commands.dispatch(gui, Inbound.FORGET_WORKSPACE, {"path": str(other)})

    assert str(other) not in last(sent, "workspace")["recents"]
    groups = [g["path"] for g in last(sent, "sessions")["workspaces"]]
    assert str(other) not in groups
    await gui.close()


async def test_forgetting_a_project_survives_a_restart(gui, sent, tmp_path):
    """Otherwise it comes back on the next start and looks like it failed."""
    from aiharness.gui.workspace import RecentWorkspaces

    other = tmp_path / "gone-project"
    other.mkdir()
    stored = RecentWorkspaces.load()
    stored.remember(other)
    stored.save()

    gui.remember_workspace(other)
    await gui_commands.dispatch(gui, Inbound.FORGET_WORKSPACE, {"path": str(other)})

    assert str(other) not in RecentWorkspaces.load().paths
    await gui.close()


async def test_remembered_projects_are_loaded_at_startup(config, workspace, tmp_path):
    """The list was only read when switching directories.

    So every restart showed a single project no matter how many had been
    opened before, which made the sidebar look broken.
    """
    from aiharness.gui.workspace import RecentWorkspaces

    earlier = tmp_path / "earlier-project"
    earlier.mkdir()
    stored = RecentWorkspaces.load()
    stored.remember(earlier)
    stored.save()

    async def send(payload):
        pass

    session = GuiSession(config, workspace, send)
    assert str(earlier) in session.recent_workspaces
    await session.close()


async def test_a_project_that_no_longer_exists_is_not_offered(config, workspace, tmp_path):
    """A deleted folder is not a shortcut, it is a click that fails."""
    from aiharness.gui.workspace import RecentWorkspaces

    stored = RecentWorkspaces.load()
    stored.remember(tmp_path / "deleted-since")
    stored.save()

    async def send(payload):
        pass

    session = GuiSession(config, workspace, send)
    assert str(tmp_path / "deleted-since") not in session.live_workspaces()
    await session.close()


async def test_the_last_project_can_be_removed(gui, sent):
    """Zero projects is a legitimate state, not one to be prevented.

    The open directory used to be pinned into the list and re-registered by
    the fallback switch, so the final project always came straight back and
    the ✕ looked broken.
    """
    # A directory becomes a project by being chosen, not by being the cwd.
    await gui_commands.dispatch(gui, Inbound.SET_WORKSPACE, {"path": str(gui.workspace)})
    assert [g["path"] for g in last(sent, "sessions")["workspaces"]] == [str(gui.workspace)]

    await gui_commands.dispatch(
        gui, Inbound.FORGET_WORKSPACE, {"path": str(gui.workspace)}
    )
    assert last(sent, "sessions")["workspaces"] == []
    assert gui.live_workspaces() == []
    await gui.close()


async def test_removing_the_open_project_leaves_the_agent_working(gui, sent):
    """Forgetting a project edits a list; it does not move the agent.

    Switching the workspace here was what re-added the directory, and it also
    silently changed which tree the open conversation could touch.
    """
    before = gui.workspace
    await gui_commands.dispatch(
        gui, Inbound.FORGET_WORKSPACE, {"path": str(gui.workspace)}
    )
    assert gui.workspace == before
    assert gui.agent.workspace == before
    await gui.close()


async def test_a_removed_project_is_still_offered_as_a_starting_point(gui, sent):
    """The chip row is how you get back, so it keeps the open directory."""
    await gui_commands.dispatch(
        gui, Inbound.FORGET_WORKSPACE, {"path": str(gui.workspace)}
    )
    workspace_msg = last(sent, "workspace")
    assert workspace_msg["path"] == str(gui.workspace)
    assert str(gui.workspace) not in workspace_msg["recents"]
    await gui.close()


async def test_removing_a_project_that_holds_the_open_session_starts_a_new_one(
    gui, sent, workspace
):
    """The conversation on screen must not survive its own deletion."""
    from aiharness.providers.base import Message

    gui.session.append(Message(role="user", content="hello"))
    doomed = gui.session.meta.id

    await gui_commands.dispatch(gui, Inbound.FORGET_WORKSPACE, {"path": str(workspace)})
    assert gui.session.meta.id != doomed
    assert gui.sessions.open(doomed) is None
    await gui.close()


async def test_starting_a_conversation_puts_a_removed_project_back(gui, sent):
    """Removal must be reversible without hunting for the folder again.

    The chip for the directory you are already in takes the "same workspace"
    path, which skipped registration entirely — so once removed, that project
    could not be restored from the UI at all.
    """
    await gui_commands.dispatch(gui, Inbound.SET_WORKSPACE, {"path": str(gui.workspace)})
    await gui_commands.dispatch(gui, Inbound.FORGET_WORKSPACE, {"path": str(gui.workspace)})
    assert gui.live_workspaces() == []

    # Exactly what clicking the chip sends.
    await gui_commands.dispatch(gui, Inbound.NEW_SESSION, {"path": str(gui.workspace)})

    assert gui.live_workspaces() == [str(gui.workspace)]
    assert [g["path"] for g in last(sent, "sessions")["workspaces"]] == [str(gui.workspace)]
    assert str(gui.workspace) in last(sent, "workspace")["recents"]
    await gui.close()


# -- one model, several accounts -------------------------------------------


async def test_the_same_model_from_a_second_account_joins_the_first(gui, sent, fake):
    """"One model, several accounts" is the shape of this config.

    Pulling k3 from a second Kimi account used to invent a separate model
    called ``k3-2``, as though the vendor had shipped a different model. The
    account is what differs, and ``k3@Kimi`` / ``k3@Kimi0018`` already says so.
    """
    from aiharness.config.schema import ProviderAccount

    gui.config.accounts.append(ProviderAccount(id="second", base_url=fake.base_url))
    await gui_commands.dispatch(
        gui, Inbound.ADD_MODEL, {"account": "primary", "model": "shared-model"}
    )
    await gui_commands.dispatch(
        gui, Inbound.ADD_MODEL, {"account": "second", "model": "shared-model"}
    )

    matching = [m for m in gui.config.models if m.model == "shared-model"]
    assert len(matching) == 1, f"expected one definition, got {[m.id for m in matching]}"
    assert matching[0].accounts == ["primary", "second"]
    await gui.close()


async def test_adding_the_same_pair_twice_changes_nothing(gui, sent):
    await gui_commands.dispatch(
        gui, Inbound.ADD_MODEL, {"account": "primary", "model": "twice"}
    )
    await gui_commands.dispatch(
        gui, Inbound.ADD_MODEL, {"account": "primary", "model": "twice"}
    )
    matching = [m for m in gui.config.models if m.model == "twice"]
    assert len(matching) == 1
    assert matching[0].accounts == ["primary"]
    await gui.close()


async def test_an_explicit_alias_still_makes_a_separate_entry(gui, sent):
    """Two configurations of one model is a real thing to want, on purpose."""
    await gui_commands.dispatch(
        gui, Inbound.ADD_MODEL, {"account": "primary", "model": "same", "alias": "cheap-one"}
    )
    await gui_commands.dispatch(
        gui, Inbound.ADD_MODEL, {"account": "primary", "model": "same", "alias": "careful-one"}
    )
    assert {m.id for m in gui.config.models if m.model == "same"} == {
        "cheap-one",
        "careful-one",
    }
    await gui.close()


# -- session isolation / rewind --------------------------------------------


async def test_empty_active_session_stays_in_the_sidebar(gui, sent):
    """A first turn must not look like '还没有会话' while it is still running."""
    # Choosing the folder registers it as a project; keep= then lists the
    # still-empty open chat instead of showing "还没有会话".
    await gui_commands.dispatch(
        gui, Inbound.SET_WORKSPACE, {"path": str(gui.workspace)}
    )
    groups = last(sent, "sessions")["workspaces"]
    active = next(g for g in groups if g["active"])
    assert any(row["id"] == gui.session.meta.id for row in active["sessions"])
    await gui.close()


async def test_can_switch_chats_while_a_turn_is_running(gui, sent, fake):
    """A live turn must keep its agent; the view can move to another chat."""
    from .fake_openai import Reply

    # Slow enough that we can switch chats before the turn finishes.
    fake.push(Reply(text="still working"))
    first_id = gui.session.meta.id
    task = asyncio.create_task(gui_commands.run_turn(gui, "long turn"))
    for _ in range(50):
        if first_id in gui.live:
            break
        await asyncio.sleep(0.02)
    assert first_id in gui.live

    await gui_commands.dispatch(gui, Inbound.NEW_SESSION, {})
    second_id = gui.session.meta.id
    assert second_id != first_id
    assert first_id in gui.live
    assert gui.session.meta.id not in gui.live

    await gui_commands.dispatch(gui, Inbound.OPEN_SESSION, {"id": first_id})
    assert gui.session.meta.id == first_id
    assert gui.agent is gui.live[first_id].agent

    await task
    assert first_id not in gui.live
    await gui.close()


async def test_opening_a_session_resets_the_todo_strip(gui, sent):
    """Todos are per chat; switching must not leave another session's list up."""
    from aiharness.providers.base import Message

    first_id = gui.session.meta.id
    gui.session_todos[first_id] = [
        {
            "content": "from session A",
            "activeForm": "doing A",
            "status": "in_progress",
        }
    ]
    gui.agent.ctx.todos = list(gui.session_todos[first_id])
    gui.session.append(Message(role="user", content="keep A"))

    await gui_commands.dispatch(gui, Inbound.NEW_SESSION, {})
    todos = last(sent, "todos")
    assert todos["todos"] == []
    assert todos["session_id"] == gui.session.meta.id

    await gui_commands.dispatch(gui, Inbound.OPEN_SESSION, {"id": first_id})
    todos = last(sent, "todos")
    assert todos["session_id"] == first_id
    assert [t["content"] for t in todos["todos"]] == ["from session A"]
    await gui.close()


async def test_ask_payload_carries_session_id(gui, sent):
    from aiharness.agent.planning import Question

    async def _ask():
        return await gui._ask_questions(
            [Question(question="Pick?", header="pick", options=[
                {"label": "A", "description": "one"},
                {"label": "B", "description": "two"},
            ])],
            session_id=gui.session.meta.id,
        )

    task = asyncio.create_task(_ask())
    await asyncio.sleep(0.05)
    ask = last(sent, "ask")
    assert ask["session_id"] == gui.session.meta.id
    assert ask["id"] in gui._pending
    gui.resolve(ask["id"], {"pick": "A"})
    assert await task == {"pick": "A"}
    await gui.close()


async def test_open_session_switches_project_without_inventing_an_empty_chat(
    gui, sent, tmp_path
):
    from aiharness.providers.base import Message

    other = tmp_path / "other-project"
    other.mkdir()
    foreign = gui.sessions.create(other, model="fake", account="primary")
    foreign.append(Message(role="user", content="from the other tree"))
    before_count = len(gui.sessions.list(workspace=other, include_empty=True))

    await gui_commands.dispatch(gui, Inbound.OPEN_SESSION, {"id": foreign.meta.id})
    assert gui.workspace == other.resolve()
    assert gui.session.meta.id == foreign.meta.id
    assert gui.agent.selection.model_id == "fake"
    after_count = len(gui.sessions.list(workspace=other, include_empty=True))
    assert after_count == before_count
    await gui.close()


async def test_rewind_turn_drops_from_the_chosen_user_message(gui, sent):
    from aiharness.providers.base import Message

    gui.session.append(Message(role="user", content="first"))
    gui.session.append(Message(role="assistant", content="a1"))
    gui.session.append(Message(role="user", content="second"))
    gui.session.append(Message(role="assistant", content="a2"))
    gui.agent._messages = list(gui.session.view())

    await gui_commands.dispatch(gui, Inbound.REWIND_TURN, {"user_index": 1})
    assert [m.content for m in gui.session.full_history] == ["first", "a1"]
    assert [m.content for m in gui.agent.messages] == ["first", "a1"]
    await gui.close()


async def test_rewind_turn_restores_pending_disk_edits(gui, sent, workspace):
    from aiharness.providers.base import Message

    target = workspace / "rewound.txt"
    target.write_text("original", encoding="utf-8")
    gui.edit_review.add(
        path=target,
        rel="rewound.txt",
        kind="write",
        before="original",
        after="changed",
        created=False,
    )
    target.write_text("changed", encoding="utf-8")
    gui.session.append(Message(role="user", content="first"))
    gui.session.append(Message(role="assistant", content="a1"))
    gui.session.append(Message(role="user", content="second"))
    gui.agent._messages = list(gui.session.view())

    await gui_commands.dispatch(gui, Inbound.REWIND_TURN, {"user_index": 1})
    assert target.read_text(encoding="utf-8") == "original"
    assert gui.edit_review.pending() == []
    await gui.close()


async def test_rewind_turn_does_not_seal_orphans_onto_truncated_log(gui, sent):
    """Sealing against pre-truncate memory used to append bare role=tool rows."""
    from aiharness.providers.base import Message, ToolCall

    gui.session.append(Message(role="user", content="first"))
    gui.session.append(Message(role="assistant", content="a1"))
    gui.session.append(Message(role="user", content="second"))
    gui.session.append(
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="t1", name="Read", arguments="{}")],
        )
    )
    # In-memory still has the unanswered tool_calls turn; session will truncate it.
    gui.agent._messages = list(gui.session.full_history)

    await gui_commands.dispatch(gui, Inbound.REWIND_TURN, {"user_index": 1})
    roles = [m.role for m in gui.session.full_history]
    assert roles == ["user", "assistant"]
    assert all(m.role != "tool" for m in gui.session.full_history)
    assert all(m.role != "tool" for m in gui.agent.messages)
    await gui.close()


async def test_turn_events_carry_session_id(gui, sent, fake):
    from .fake_openai import Reply

    fake.push(Reply(text="hello"))
    await gui_commands.run_turn(gui, "say hi")
    start = last(sent, "turn_start")
    assert start["session_id"] == gui.session.meta.id
    texts = [m for m in sent if m["type"] == "text"]
    assert texts and all(m.get("session_id") == gui.session.meta.id for m in texts)
    await gui.close()


async def test_run_turn_hard_cancel_emits_terminal_done(gui, sent, monkeypatch):
    """Force-cancel must push DONE so the UI leaves '正在打断…'."""

    async def hang(_self, _text):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise
        if False:  # pragma: no cover — keep this an async generator
            yield

    monkeypatch.setattr(type(gui.agent), "run", hang)
    task = asyncio.create_task(gui_commands.run_turn(gui, "hi"))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if gui.session.meta.id in gui.live:
            break
    assert gui.session.meta.id in gui.live
    await gui_commands.dispatch(gui, Inbound.INTERRUPT, {"force": True})
    with pytest.raises(asyncio.CancelledError):
        await task
    done = last(sent, "done")
    assert done["interrupted"] is True
    assert gui.session.meta.id not in gui.live
    await gui.close()


async def test_detach_client_keeps_live_turns(gui, sent):
    from aiharness.gui.bridge import LiveTurn

    sid = gui.session.meta.id
    gui.live[sid] = LiveTurn(
        session_id=sid, handle=gui.session, agent=gui.agent, task=None
    )
    await gui.detach_client()
    assert sid in gui.live
    assert gui._client_detached is True
    # Re-attach must restore push without recreating the runtime.
    async def send(payload):
        sent.append(payload)

    gui.bind_send(send)
    await gui.on_client_attached()
    assert last(sent, "status")["status"]["busy"] is True
    gui.live.pop(sid, None)
    await gui.close()


async def test_edit_boards_are_isolated_per_session(gui):
    a = gui.edit_board("session-a")
    b = gui.edit_board("session-b")
    assert a is not b
    assert gui.edit_board("session-a") is a
    await gui.close()


async def test_todos_persist_across_rebuild(gui, workspace):
    todos = [
        {"content": "one", "status": "completed", "activeForm": "one"},
        {"content": "two", "status": "in_progress", "activeForm": "two"},
    ]
    gui.session.save_todos(todos)
    reopened = gui.sessions.open(gui.session.meta.id)
    assert reopened is not None
    assert [t["content"] for t in reopened.todos] == ["one", "two"]
    assert reopened.todos[1]["status"] == "in_progress"
    await gui.close()


async def test_continue_work_starts_turn_from_open_todos(gui, sent, fake):
    from .fake_openai import Reply

    gui.session.save_todos(
        [{"content": "finish docs", "status": "pending", "activeForm": "docs"}]
    )
    gui.agent.ctx.todos = list(gui.session.todos)
    gui.session_todos[gui.session.meta.id] = list(gui.session.todos)
    fake.push(Reply(text="continued"))
    await gui_commands.dispatch(gui, Inbound.CONTINUE_WORK, {})
    assert gui._turn_task is not None
    await gui._turn_task
    assert any(m.get("type") == "turn_start" for m in sent)
    # Continue must not re-classify (avoids AskUser park after "例行任务").
    assert not any(
        "例行" in str(m.get("text") or "") or "Project" in str(m.get("text") or "")
        for m in sent
        if m.get("type") == "notice"
    )
    await gui.close()


async def test_interrupt_idle_does_not_claim_interrupted(gui, sent):
    await gui_commands.dispatch(gui, Inbound.INTERRUPT, {})
    notice = last(sent, "notice")
    assert "没有进行中" in notice["text"] or "Nothing is running" in notice["text"]
    await gui.close()


async def test_quest_files_are_session_scoped(tmp_path):
    from aiharness.quest import load_quest, quest_path, start_quest

    start_quest(tmp_path, "A", ["step"], session_id="chat-a")
    start_quest(tmp_path, "B", ["step"], session_id="chat-b")
    assert quest_path(tmp_path, session_id="chat-a") != quest_path(
        tmp_path, session_id="chat-b"
    )
    assert load_quest(tmp_path, session_id="chat-a").goal == "A"
    assert load_quest(tmp_path, session_id="chat-b").goal == "B"
