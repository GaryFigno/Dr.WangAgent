"""Tests for the local Responses → Chat Completions bridge."""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web

from aiharness.gui.responses_bridge import (
    CLIENT_MAX_BODY_BYTES,
    ResponsesBridge,
    _norm_upstream,
    needs_responses_bridge,
    responses_body_to_chat,
    responses_input_to_messages,
)


def test_needs_responses_bridge_kimi_coding():
    assert needs_responses_bridge("https://api.kimi.com/coding/v1")
    assert needs_responses_bridge("https://api.kimi.com/coding")
    assert not needs_responses_bridge("https://api.moonshot.cn/v1")
    assert not needs_responses_bridge("https://api.deepseek.com/v1")


def test_bridge_does_not_cap_request_bodies():
    # Passthrough: match CLI — no artificial 1 MiB aiohttp default.
    assert CLIENT_MAX_BODY_BYTES == 0


def test_norm_upstream_adds_v1_for_kimi_coding():
    assert _norm_upstream("https://api.kimi.com/coding") == "https://api.kimi.com/coding/v1"
    assert _norm_upstream("https://api.kimi.com/coding/v1") == "https://api.kimi.com/coding/v1"
    assert _norm_upstream("https://api.deepseek.com/v1") == "https://api.deepseek.com/v1"


def test_responses_input_to_messages_string_and_list():
    assert responses_input_to_messages({"input": "hi"}) == [
        {"role": "user", "content": "hi"}
    ]
    body = {
        "instructions": "be brief",
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]}
        ],
    }
    messages = responses_input_to_messages(body)
    assert messages[0] == {"role": "system", "content": "be brief"}
    assert messages[1]["role"] == "user"
    assert "hello" in str(messages[1]["content"])


def test_developer_role_mapped_to_system():
    """Kimi Coding rejects role=developer; Codex uses it for instructions."""
    from aiharness.gui.responses_bridge import responses_body_to_chat

    chat = responses_body_to_chat(
        {
            "model": "k3",
            "input": [
                {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "sys"}]},
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            ],
        }
    )
    assert chat["messages"][0]["role"] == "system"
    assert "sys" in str(chat["messages"][0]["content"])
    assert chat["messages"][1]["role"] == "user"


def test_sanitize_drops_empty_assistant_before_tool_calls():
    """Kimi 400: assistant message must not be empty (Codex reasoning placeholder)."""
    from aiharness.gui.responses_bridge import sanitize_chat_messages

    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "ok"},
        {"role": "assistant", "content": "", "tool_calls": None},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "shell", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "done"},
    ]
    cleaned = sanitize_chat_messages(messages)
    assistants = [m for m in cleaned if m["role"] == "assistant"]
    assert len(assistants) == 1
    assert assistants[0]["content"] == "ok"
    assert len(assistants[0]["tool_calls"]) == 1
    assert cleaned[-1]["role"] == "tool"


def test_responses_body_to_chat_maps_stream_and_model():
    chat = responses_body_to_chat(
        {"model": "k3", "input": "ping", "stream": True, "max_output_tokens": 128}
    )
    assert chat["model"] == "k3"
    assert chat["stream"] is True
    assert chat["max_tokens"] == 128
    assert chat["messages"][0]["content"] == "ping"


@pytest.mark.asyncio
async def test_bridge_stream_text_via_fake_upstream():
    async def chat_completions(request: web.Request) -> web.StreamResponse:
        body = await request.json()
        assert body["model"] == "k3"
        assert body["stream"] is True
        assert request.headers.get("Authorization") == "Bearer test-key"
        resp = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream; charset=utf-8"},
        )
        await resp.prepare(request)
        for piece in ("Hel", "lo"):
            chunk = {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "choices": [{"delta": {"content": piece}, "index": 0}],
            }
            await resp.write(f"data: {json.dumps(chunk)}\n\n".encode("utf-8"))
            await asyncio.sleep(0)
        await resp.write(b"data: [DONE]\n\n")
        await resp.write_eof()
        return resp

    async def models(request: web.Request) -> web.Response:
        return web.json_response({"data": [{"id": "k3"}]})

    upstream = web.Application()
    upstream.router.add_post("/v1/chat/completions", chat_completions)
    upstream.router.add_get("/v1/models", models)
    runner = web.AppRunner(upstream, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    upstream_base = f"http://127.0.0.1:{port}/v1"

    bridge = ResponsesBridge()
    try:
        local = await bridge.start(upstream_base, "test-key")
        assert local.startswith("http://127.0.0.1:")
        assert local.endswith("/v1")

        from aiohttp import ClientSession

        async with ClientSession() as session:
            models_resp = await session.get(f"{local}/models")
            assert models_resp.status == 200
            models_payload = await models_resp.json()
            assert models_payload["data"][0]["id"] == "k3"

            async with session.post(
                f"{local}/responses",
                json={"model": "k3", "input": "ping", "stream": True},
                headers={"Authorization": "Bearer test-key"},
            ) as resp:
                assert resp.status == 200
                text = await resp.text()
        assert "response.created" in text
        assert "response.output_text.delta" in text
        assert "Hel" in text and "lo" in text
        assert "response.completed" in text
    finally:
        await bridge.stop()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_bridge_upstream_error_still_emits_terminal_events():
    async def chat_completions(request: web.Request) -> web.Response:
        return web.json_response({"error": {"message": "bad model"}}, status=400)

    upstream = web.Application()
    upstream.router.add_post("/v1/chat/completions", chat_completions)
    runner = web.AppRunner(upstream, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    bridge = ResponsesBridge()
    try:
        local = await bridge.start(f"http://127.0.0.1:{port}/v1", "k")
        from aiohttp import ClientSession

        async with ClientSession() as session:
            async with session.post(
                f"{local}/responses",
                json={"model": "bad", "input": "ping", "stream": True},
            ) as resp:
                text = await resp.text()
        assert "response.created" in text
        assert "response.failed" in text or "response.completed" in text
        assert "bad model" in text
    finally:
        await bridge.stop()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_bridge_empty_body_does_not_hard_fail():
    async def chat_completions(request: web.Request) -> web.Response:
        return web.json_response({"error": {"message": "unused"}}, status=500)

    upstream = web.Application()
    upstream.router.add_post("/v1/chat/completions", chat_completions)
    runner = web.AppRunner(upstream, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    bridge = ResponsesBridge()
    try:
        local = await bridge.start(f"http://127.0.0.1:{port}/v1", "k")
        from aiohttp import ClientSession

        async with ClientSession() as session:
            async with session.post(f"{local}/responses", data=b"") as resp:
                assert resp.status == 200
                payload = await resp.json()
        assert payload["status"] == "completed"
        assert payload["output"] == []
    finally:
        await bridge.stop()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_bridge_non_stream():
    async def chat_completions(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "id": "chatcmpl-x",
                "choices": [{"message": {"role": "assistant", "content": "pong"}}],
            }
        )

    upstream = web.Application()
    upstream.router.add_post("/v1/chat/completions", chat_completions)
    runner = web.AppRunner(upstream, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    bridge = ResponsesBridge()
    try:
        local = await bridge.start(f"http://127.0.0.1:{port}/v1", "k")
        from aiohttp import ClientSession

        async with ClientSession() as session:
            async with session.post(
                f"{local}/responses",
                json={"model": "k3", "input": "ping", "stream": False},
            ) as resp:
                assert resp.status == 200
                payload = await resp.json()
        assert payload["object"] == "response"
        assert payload["status"] == "completed"
        assert payload["output"][0]["content"][0]["text"] == "pong"
    finally:
        await bridge.stop()
        await runner.cleanup()
