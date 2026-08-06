"""A scriptable fake of an OpenAI-compatible chat-completions endpoint.

Tests push canned responses onto :attr:`FakeOpenAI.script`; each request pops
the next one. A response may be a normal completion, a tool call, or an HTTP
error, which is how failover is exercised.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

#: Tokens reported for every scripted response, so cost maths is predictable.
FAKE_PROMPT_TOKENS = 100
FAKE_COMPLETION_TOKENS = 20
FAKE_CACHED_TOKENS = 40


@dataclass
class Reply:
    """One scripted response."""

    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    reasoning: str = ""
    status: int = 200
    error: str = ""
    cached_tokens: int = FAKE_CACHED_TOKENS


@dataclass
class Recorded:
    """What the server saw."""

    path: str
    authorization: str
    body: dict[str, Any]


class _Handler(BaseHTTPRequestHandler):
    server: FakeServer  # type: ignore[assignment]

    def log_message(self, *args: Any) -> None:  # noqa: A003 - silence the default logger
        pass

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path.endswith("/models"):
            self._send_json(200, {"data": [{"id": "fake-model"}]})
            return
        self._send_json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {}

        owner = self.server.owner
        owner.requests.append(
            Recorded(
                path=self.path,
                authorization=self.headers.get("Authorization", ""),
                body=body,
            )
        )

        reply = owner.next_reply()
        if reply.status != 200:
            self._send_json(reply.status, {"error": {"message": reply.error or "boom"}})
            return
        if body.get("stream"):
            self._send_stream(reply)
        else:
            self._send_json(200, _completion_payload(reply))

    # -- transport helpers ------------------------------------------------

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_stream(self, reply: Reply) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in _stream_chunks(reply):
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def _usage(reply: Reply) -> dict[str, Any]:
    return {
        "prompt_tokens": FAKE_PROMPT_TOKENS,
        "completion_tokens": FAKE_COMPLETION_TOKENS,
        "total_tokens": FAKE_PROMPT_TOKENS + FAKE_COMPLETION_TOKENS,
        "prompt_tokens_details": {"cached_tokens": reply.cached_tokens},
    }


def _completion_payload(reply: Reply) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": reply.text}
    if reply.reasoning:
        message["reasoning_content"] = reply.reasoning
    if reply.tool_calls:
        message["tool_calls"] = reply.tool_calls
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "model": "fake-model",
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if reply.tool_calls else "stop",
            }
        ],
        "usage": _usage(reply),
    }


def _stream_chunks(reply: Reply) -> list[dict[str, Any]]:
    """Split a scripted reply into plausible SSE chunks."""
    chunks: list[dict[str, Any]] = []

    def frame(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
        return {
            "id": "chatcmpl-fake",
            "object": "chat.completion.chunk",
            "model": "fake-model",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    if reply.reasoning:
        chunks.append(frame({"reasoning_content": reply.reasoning}))
    # Emit the text a few characters at a time to exercise accumulation.
    for start in range(0, len(reply.text), 8):
        chunks.append(frame({"content": reply.text[start : start + 8]}))
    for index, call in enumerate(reply.tool_calls):
        function = call.get("function", {})
        chunks.append(
            frame(
                {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": call.get("id", f"call_{index}"),
                            "type": "function",
                            "function": {"name": function.get("name", "")},
                        }
                    ]
                }
            )
        )
        arguments = function.get("arguments", "{}")
        midpoint = len(arguments) // 2
        for piece in (arguments[:midpoint], arguments[midpoint:]):
            chunks.append(
                frame(
                    {
                        "tool_calls": [
                            {"index": index, "function": {"arguments": piece}}
                        ]
                    }
                )
            )
    chunks.append(frame({}, "tool_calls" if reply.tool_calls else "stop"))
    chunks.append({"id": "chatcmpl-fake", "choices": [], "usage": _usage(reply)})
    return chunks


class FakeServer(ThreadingHTTPServer):
    owner: FakeOpenAI
    daemon_threads = True


class FakeOpenAI:
    """A running fake endpoint. Use as a context manager."""

    def __init__(self) -> None:
        self.script: list[Reply] = []
        self.requests: list[Recorded] = []
        self.default = Reply(text="ok")
        self._server: FakeServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def next_reply(self) -> Reply:
        with self._lock:
            if self.script:
                return self.script.pop(0)
            return self.default

    def push(self, *replies: Reply) -> FakeOpenAI:
        with self._lock:
            self.script.extend(replies)
        return self

    @property
    def base_url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return f"http://127.0.0.1:{port}/v1"

    def __enter__(self) -> FakeOpenAI:
        self._server = FakeServer(("127.0.0.1", 0), _Handler)
        self._server.owner = self
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


def tool_call(name: str, arguments: dict[str, Any], call_id: str = "call_1") -> dict[str, Any]:
    """Build a tool-call payload for a scripted reply."""
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }
