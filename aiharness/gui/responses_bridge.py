"""Local OpenAI Responses → Chat Completions bridge for Codex.

Codex (2026+) requires ``wire_api=responses`` (``POST /v1/responses``). Some
providers — notably Kimi Coding — only expose Chat Completions. This tiny
loopback proxy accepts Responses requests and forwards them as chat/completions,
translating streaming SSE back into the Responses event shapes Codex expects.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any
from urllib.parse import urlparse

from aiohttp import ClientSession, ClientTimeout, TCPConnector, web

from ..providers import proxy as proxy_mod

log = logging.getLogger(__name__)

HOST = "127.0.0.1"
#: aiohttp defaults to 1 MiB which invents a limit the real CLI does not have.
#: 0 = unlimited — we are a passthrough, not a size gate.
CLIENT_MAX_BODY_BYTES = 0


def needs_responses_bridge(base_url: str) -> bool:
    """True when the endpoint is chat-only (Kimi Coding)."""
    raw = (base_url or "").strip()
    if not raw:
        return False
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parsed = urlparse(raw)
    except Exception:  # noqa: BLE001
        return False
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    return "api.kimi.com" in host and "coding" in path


def _norm_upstream(base: str) -> str:
    text = (base or "").strip().rstrip("/")
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text
    try:
        parsed = urlparse(text)
        host = (parsed.hostname or "").lower()
        path = (parsed.path or "").rstrip("/").lower()
    except Exception:  # noqa: BLE001
        return text
    # Kimi Coding chat completions live under /coding/v1.
    if "api.kimi.com" in host and "coding" in path and not path.endswith("/v1"):
        text = text.rstrip("/") + "/v1"
    return text


def responses_input_to_messages(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Map a Responses request body into chat ``messages``."""
    messages: list[dict[str, Any]] = []
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})

    raw_input = body.get("input")
    if isinstance(raw_input, str):
        if raw_input:
            messages.append({"role": "user", "content": raw_input})
        return messages or [{"role": "user", "content": ""}]

    if not isinstance(raw_input, list):
        return messages or [{"role": "user", "content": ""}]

    pending_tool_calls: list[dict[str, Any]] = []

    def _flush_tool_calls() -> None:
        nonlocal pending_tool_calls
        if not pending_tool_calls:
            return
        messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": pending_tool_calls,
            }
        )
        pending_tool_calls = []

    for item in raw_input:
        if isinstance(item, str):
            _flush_tool_calls()
            if item:
                messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "")
        if item_type in {"function_call", "custom_tool_call"}:
            call_id = str(item.get("call_id") or item.get("id") or "")
            name = str(item.get("name") or "")
            arguments = item.get("arguments")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments or {}, ensure_ascii=False)
            pending_tool_calls.append(
                {
                    "id": call_id or f"call_{uuid.uuid4().hex[:12]}",
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }
            )
            continue
        _flush_tool_calls()
        # Already chat-shaped.
        if "role" in item and ("content" in item or "parts" in item):
            role = _chat_role(item.get("role") or "user")
            content = item.get("content")
            if content is None:
                content = item.get("parts")
            messages.append({"role": role, "content": _flatten_content(content)})
            continue
        if item_type in {"message", "input_text", "output_text"} or "role" in item:
            role = _chat_role(
                item.get("role")
                or ("assistant" if item_type == "output_text" else "user")
            )
            content = item.get("content", item.get("text", ""))
            messages.append({"role": role, "content": _flatten_content(content)})
            continue
        if item_type == "function_call_output":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(item.get("call_id") or item.get("id") or ""),
                    "content": _flatten_content(item.get("output") or item.get("content") or ""),
                }
            )
    _flush_tool_calls()
    return messages or [{"role": "user", "content": ""}]


def _chat_role(role: Any) -> str:
    """Map Responses / Codex roles onto Chat Completions roles Kimi accepts.

    Kimi Coding rejects ``developer`` with HTTP 400
    (``role 'developer' is not allowed``). Codex uses that role for system
    instructions, so remap to ``system``.
    """
    value = str(role or "user").strip().lower()
    if value in {"developer", "system"}:
        return "system"
    if value in {"assistant", "model"}:
        return "assistant"
    if value in {"tool", "function"}:
        return "tool"
    return "user"


def _flatten_content(content: Any) -> Any:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        parts: list[dict[str, Any]] = []
        for part in content:
            if isinstance(part, str):
                texts.append(part)
                continue
            if not isinstance(part, dict):
                continue
            ptype = str(part.get("type") or "")
            if ptype in {"input_text", "output_text", "text"}:
                texts.append(str(part.get("text") or ""))
            elif ptype in {"input_image", "image_url"}:
                image = part.get("image_url") or part.get("url") or part
                if isinstance(image, str):
                    parts.append({"type": "image_url", "image_url": {"url": image}})
                elif isinstance(image, dict):
                    parts.append({"type": "image_url", "image_url": image})
            else:
                text = part.get("text")
                if text:
                    texts.append(str(text))
        if parts:
            out: list[dict[str, Any]] = []
            if texts:
                out.append({"type": "text", "text": "".join(texts)})
            out.extend(parts)
            return out
        return "".join(texts)
    if content is None:
        return ""
    return str(content)


def _content_is_empty(content: Any) -> bool:
    if content is None:
        return True
    if isinstance(content, str):
        return not content.strip()
    if isinstance(content, list):
        return len(content) == 0
    return False


def sanitize_chat_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop / merge history shapes that Kimi Coding rejects.

    Codex often emits an empty assistant message (reasoning-only / placeholder)
    immediately before a tool-call assistant. Kimi returns HTTP 400:
    ``the message at position N with role 'assistant' must not be empty``.
    """
    cleaned: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = _chat_role(message.get("role"))
        item = {**message, "role": role}
        tool_calls = item.get("tool_calls")
        has_tools = isinstance(tool_calls, list) and len(tool_calls) > 0
        if role == "assistant" and _content_is_empty(item.get("content")) and not has_tools:
            continue
        if role == "assistant" and has_tools and item.get("content") is None:
            item["content"] = ""
        # Merge text-only assistant + following tool-call assistant into one turn.
        if (
            role == "assistant"
            and has_tools
            and cleaned
            and cleaned[-1].get("role") == "assistant"
            and not cleaned[-1].get("tool_calls")
            and not _content_is_empty(cleaned[-1].get("content"))
        ):
            prior = cleaned[-1]
            merged = {
                "role": "assistant",
                "content": prior.get("content") or "",
                "tool_calls": tool_calls,
            }
            cleaned[-1] = merged
            continue
        cleaned.append(item)
    return cleaned or [{"role": "user", "content": ""}]


def responses_body_to_chat(body: dict[str, Any]) -> dict[str, Any]:
    """Build a chat/completions payload from a Responses request."""
    messages = responses_input_to_messages(body)
    # Second pass: coerce any role Codex may have stuffed into chat-shaped items.
    for message in messages:
        if isinstance(message, dict) and "role" in message:
            message["role"] = _chat_role(message.get("role"))
    messages = sanitize_chat_messages(messages)
    chat: dict[str, Any] = {
        "model": body.get("model") or "",
        "messages": messages,
        "stream": bool(body.get("stream")),
    }
    for key in ("temperature", "top_p", "max_tokens", "max_output_tokens", "tools", "tool_choice"):
        if key not in body:
            continue
        value = body[key]
        if key == "max_output_tokens":
            chat["max_tokens"] = value
        else:
            chat[key] = value
    # Drop Responses-only noise; keep tools if already OpenAI-shaped.
    tools = chat.get("tools")
    if isinstance(tools, list):
        normalised = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
                normalised.append(tool)
            elif "name" in tool and "parameters" in tool:
                normalised.append({"type": "function", "function": tool})
        if normalised:
            chat["tools"] = normalised
        else:
            chat.pop("tools", None)
    return chat


def chat_completion_to_response(chat: dict[str, Any], *, model: str) -> dict[str, Any]:
    """Minimal non-stream Responses object from a chat completion."""
    choice = {}
    choices = chat.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        choice = choices[0]
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    text = str(message.get("content") or "")
    response_id = f"resp_{uuid.uuid4().hex[:24]}"
    item_id = f"msg_{uuid.uuid4().hex[:24]}"
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": model or str(chat.get("model") or ""),
        "output": [
            {
                "id": item_id,
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "usage": chat.get("usage") or {},
    }


def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


class ResponsesBridge:
    """Ephemeral loopback server: Responses in, Chat Completions upstream."""

    def __init__(self) -> None:
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._session: ClientSession | None = None
        self.upstream_base = ""
        self.api_key = ""
        self.proxy = ""
        self.port = 0
        self.base_url = ""
        self.last_error = ""

    async def start(self, upstream_base: str, api_key: str, proxy: str = "") -> str:
        """Start listening; return local OpenAI-style base URL (…/v1)."""
        await self.stop()
        self.upstream_base = _norm_upstream(upstream_base)
        self.api_key = (api_key or "").strip()
        try:
            self.proxy = proxy_mod.normalise(proxy or "")
        except proxy_mod.ProxyError:
            self.proxy = ""

        connector = TCPConnector(force_close=True)
        timeout = ClientTimeout(total=None, sock_connect=30, sock_read=None)
        trust_env = not self.proxy
        self._session = ClientSession(connector=connector, timeout=timeout, trust_env=trust_env)

        app = web.Application(client_max_size=CLIENT_MAX_BODY_BYTES)
        app.router.add_route("*", "/v1/responses", self._handle_responses)
        app.router.add_route("*", "/responses", self._handle_responses)
        app.router.add_get("/v1/models", self._handle_models)
        app.router.add_get("/models", self._handle_models)
        app.router.add_get("/health", self._handle_health)

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, HOST, 0)
        await self._site.start()
        sockets = getattr(self._site, "_server", None)
        if sockets is not None and getattr(sockets, "sockets", None):
            self.port = int(sockets.sockets[0].getsockname()[1])
        else:
            # Fallback: inspect runner sites.
            for site in self._runner.sites:
                server = getattr(site, "_server", None)
                if server and getattr(server, "sockets", None):
                    self.port = int(server.sockets[0].getsockname()[1])
                    break
        if not self.port:
            raise RuntimeError("ResponsesBridge failed to bind a local port")
        self.base_url = f"http://{HOST}:{self.port}/v1"
        log.info("ResponsesBridge listening on %s → %s", self.base_url, self.upstream_base)
        return self.base_url

    async def stop(self) -> None:
        if self._site is not None:
            try:
                await self._site.stop()
            except Exception:  # noqa: BLE001
                pass
            self._site = None
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:  # noqa: BLE001
                pass
            self._runner = None
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:  # noqa: BLE001
                pass
            self._session = None
        self.port = 0
        self.base_url = ""

    def _upstream_url(self, suffix: str) -> str:
        base = self.upstream_base.rstrip("/")
        path = suffix if suffix.startswith("/") else f"/{suffix}"
        return f"{base}{path}"

    def _auth_headers(self, request: web.Request) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        auth = request.headers.get("Authorization") or ""
        if auth:
            headers["Authorization"] = auth
        elif self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _proxy_kw(self) -> dict[str, Any]:
        if self.proxy and self.proxy != proxy_mod.DIRECT:
            return {"proxy": self.proxy}
        return {}

    async def _handle_health(self, _request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "upstream": self.upstream_base})

    async def _handle_models(self, request: web.Request) -> web.StreamResponse:
        assert self._session is not None
        try:
            async with self._session.get(
                self._upstream_url("/models"),
                headers=self._auth_headers(request),
                **self._proxy_kw(),
            ) as resp:
                body = await resp.read()
                return web.Response(
                    body=body,
                    status=resp.status,
                    content_type=resp.content_type or "application/json",
                )
        except Exception as error:  # noqa: BLE001
            log.warning("ResponsesBridge /models failed: %s", error)
            return web.json_response({"error": {"message": str(error)}}, status=502)

    async def _handle_responses(self, request: web.Request) -> web.StreamResponse:
        if request.method == "OPTIONS":
            return web.Response(status=204)
        if request.method != "POST":
            return web.json_response({"error": {"message": "POST required"}}, status=405)
        assert self._session is not None
        try:
            raw = await request.read()
        except Exception as error:  # noqa: BLE001
            err_name = type(error).__name__
            tip = str(error)
            if "too large" in tip.lower() or err_name == "HTTPRequestEntityTooLarge":
                tip = (
                    "Request Entity Too Large — 上游或代理拒绝了过大的请求体。"
                    "请新开 Codex 会话后再继续。"
                )
            log.warning("ResponsesBridge body read failed: %s", tip)
            self.last_error = tip
            return web.json_response(
                {"error": {"message": tip}},
                status=413,
            )
        if not raw or not raw.strip():
            # Codex sometimes probes /responses with an empty POST; answer softly
            # instead of poisoning the turn with a hard "invalid JSON" failure.
            log.warning("ResponsesBridge empty body from %s", request.remote)
            return web.json_response(
                {
                    "id": f"resp_empty_{uuid.uuid4().hex[:10]}",
                    "object": "response",
                    "created_at": int(time.time()),
                    "status": "completed",
                    "model": "",
                    "output": [],
                }
            )
        text = raw.decode("utf-8-sig", errors="replace").strip()
        try:
            body = json.loads(text)
        except json.JSONDecodeError as error:
            snippet = text[:240].replace("\n", "\\n")
            log.warning(
                "ResponsesBridge invalid JSON (%s): %s",
                error,
                snippet,
            )
            self.last_error = f"invalid JSON: {error} · {snippet}"
            return web.json_response(
                {"error": {"message": f"invalid JSON: {error}"}},
                status=400,
            )
        if not isinstance(body, dict):
            return web.json_response({"error": {"message": "object required"}}, status=400)

        chat_body = responses_body_to_chat(body)
        stream = bool(chat_body.get("stream"))
        headers = self._auth_headers(request)
        url = self._upstream_url("/chat/completions")

        if not stream:
            try:
                async with self._session.post(
                    url,
                    json=chat_body,
                    headers=headers,
                    **self._proxy_kw(),
                ) as resp:
                    payload = await resp.json(content_type=None)
                    if resp.status >= 400:
                        return web.json_response(payload if isinstance(payload, dict) else {"error": payload}, status=resp.status)
                    if not isinstance(payload, dict):
                        return web.json_response({"error": {"message": "bad upstream"}}, status=502)
                    return web.json_response(
                        chat_completion_to_response(payload, model=str(chat_body.get("model") or ""))
                    )
            except Exception as error:  # noqa: BLE001
                log.warning("ResponsesBridge non-stream failed: %s", error)
                return web.json_response({"error": {"message": str(error)}}, status=502)

        return await self._stream_responses(request, url, chat_body, headers)

    async def _stream_responses(
        self,
        request: web.Request,
        url: str,
        chat_body: dict[str, Any],
        headers: dict[str, str],
    ) -> web.StreamResponse:
        assert self._session is not None
        response_id = f"resp_{uuid.uuid4().hex[:24]}"
        item_id = f"msg_{uuid.uuid4().hex[:24]}"
        model = str(chat_body.get("model") or "")
        created = int(time.time())

        out = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream; charset=utf-8",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
        await out.prepare(request)

        base_response = {
            "id": response_id,
            "object": "response",
            "created_at": created,
            "status": "in_progress",
            "model": model,
            "output": [],
        }
        await out.write(_sse("response.created", {"type": "response.created", "response": base_response}))
        await out.write(
            _sse(
                "response.in_progress",
                {"type": "response.in_progress", "response": base_response},
            )
        )
        await out.write(
            _sse(
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {
                        "id": item_id,
                        "type": "message",
                        "role": "assistant",
                        "status": "in_progress",
                        "content": [],
                    },
                },
            )
        )
        await out.write(
            _sse(
                "response.content_part.added",
                {
                    "type": "response.content_part.added",
                    "item_id": item_id,
                    "output_index": 0,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": ""},
                },
            )
        )

        text_parts: list[str] = []
        tool_acc: dict[int, dict[str, str]] = {}
        failed_message = ""
        try:
            async with self._session.post(
                url,
                json=chat_body,
                headers=headers,
                **self._proxy_kw(),
            ) as resp:
                if resp.status >= 400:
                    err_body = await resp.text()
                    failed_message = f"upstream {resp.status}: {err_body[:500]}"
                    log.warning("ResponsesBridge upstream error: %s", failed_message)
                    self.last_error = failed_message
                else:
                    buffer = ""
                    async for raw in resp.content.iter_any():
                        if request.transport is None or request.transport.is_closing():
                            break
                        buffer += raw.decode("utf-8", errors="replace")
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            line = line.strip()
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if not data or data == "[DONE]":
                                continue
                            try:
                                chunk = json.loads(data)
                            except json.JSONDecodeError:
                                continue
                            if not isinstance(chunk, dict):
                                continue
                            _accumulate_tool_calls(chunk, tool_acc)
                            delta_text = _chat_delta_text(chunk)
                            if not delta_text:
                                continue
                            text_parts.append(delta_text)
                            await out.write(
                                _sse(
                                    "response.output_text.delta",
                                    {
                                        "type": "response.output_text.delta",
                                        "item_id": item_id,
                                        "output_index": 0,
                                        "content_index": 0,
                                        "delta": delta_text,
                                    },
                                )
                            )
        except Exception as error:  # noqa: BLE001
            failed_message = str(error)
            log.warning("ResponsesBridge stream failed: %s", error)

        if failed_message:
            await out.write(
                _sse(
                    "error",
                    {
                        "type": "error",
                        "error": {"message": failed_message},
                    },
                )
            )
            # Codex retries when the SSE ends without a terminal response event.
            failed = {
                "id": response_id,
                "object": "response",
                "created_at": created,
                "status": "failed",
                "model": model,
                "output": [],
                "error": {"message": failed_message},
            }
            await out.write(
                _sse("response.failed", {"type": "response.failed", "response": failed})
            )
            # Also emit completed-shaped terminal for older clients that only watch completed.
            await out.write(
                _sse(
                    "response.completed",
                    {
                        "type": "response.completed",
                        "response": {**failed, "status": "incomplete"},
                    },
                )
            )
            await out.write_eof()
            return out

        full_text = "".join(text_parts)
        tool_calls = [
            tool_acc[index]
            for index in sorted(tool_acc)
            if tool_acc[index].get("name")
        ]
        await out.write(
            _sse(
                "response.output_text.done",
                {
                    "type": "response.output_text.done",
                    "item_id": item_id,
                    "output_index": 0,
                    "content_index": 0,
                    "text": full_text,
                },
            )
        )
        await out.write(
            _sse(
                "response.content_part.done",
                {
                    "type": "response.content_part.done",
                    "item_id": item_id,
                    "output_index": 0,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": full_text},
                },
            )
        )
        await out.write(
            _sse(
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {
                        "id": item_id,
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": full_text}],
                    },
                },
            )
        )
        output: list[dict[str, Any]] = [
            {
                "id": item_id,
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": full_text}],
            }
        ]
        for index, call in enumerate(tool_calls, start=1):
            call_id = call.get("id") or f"call_{uuid.uuid4().hex[:12]}"
            fn_item = {
                "id": f"fc_{uuid.uuid4().hex[:16]}",
                "type": "function_call",
                "status": "completed",
                "call_id": call_id,
                "name": call.get("name") or "",
                "arguments": call.get("arguments") or "{}",
            }
            await out.write(
                _sse(
                    "response.output_item.added",
                    {
                        "type": "response.output_item.added",
                        "output_index": index,
                        "item": {**fn_item, "status": "in_progress", "arguments": ""},
                    },
                )
            )
            if fn_item["arguments"]:
                await out.write(
                    _sse(
                        "response.function_call_arguments.delta",
                        {
                            "type": "response.function_call_arguments.delta",
                            "item_id": fn_item["id"],
                            "output_index": index,
                            "delta": fn_item["arguments"],
                        },
                    )
                )
            await out.write(
                _sse(
                    "response.function_call_arguments.done",
                    {
                        "type": "response.function_call_arguments.done",
                        "item_id": fn_item["id"],
                        "output_index": index,
                        "arguments": fn_item["arguments"],
                    },
                )
            )
            await out.write(
                _sse(
                    "response.output_item.done",
                    {
                        "type": "response.output_item.done",
                        "output_index": index,
                        "item": fn_item,
                    },
                )
            )
            output.append(fn_item)
        completed = {
            "id": response_id,
            "object": "response",
            "created_at": created,
            "status": "completed",
            "model": model,
            "output": output,
        }
        await out.write(
            _sse("response.completed", {"type": "response.completed", "response": completed})
        )
        await out.write_eof()
        return out


def _chat_delta_text(chunk: dict[str, Any]) -> str:
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    choice = choices[0]
    if not isinstance(choice, dict):
        return ""
    delta = choice.get("delta")
    if isinstance(delta, dict):
        content = delta.get("content")
        if isinstance(content, str):
            return content
    message = choice.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content
    return ""


def _accumulate_tool_calls(chunk: dict[str, Any], acc: dict[int, dict[str, str]]) -> None:
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return
    delta = choices[0].get("delta")
    if not isinstance(delta, dict):
        return
    tool_calls = delta.get("tool_calls")
    if not isinstance(tool_calls, list):
        return
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        index = int(call.get("index") or 0)
        bucket = acc.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if call.get("id"):
            bucket["id"] = str(call["id"])
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        if function.get("name"):
            bucket["name"] = str(function["name"])
        if function.get("arguments"):
            bucket["arguments"] = bucket.get("arguments", "") + str(function["arguments"])
