"""MCP transports.

MCP is JSON-RPC 2.0 carried over one of two links:

* **stdio** — the server is a child process; requests go to its stdin, one
  JSON object per line, and responses come back on stdout. This is what
  almost every local server uses.
* **streamable HTTP** — requests are POSTed; the response is either a single
  JSON object or an SSE stream. Used by hosted servers.

Both are reduced here to the same interface: send a JSON object, get one
back. Server-initiated notifications are read and discarded, because this
client does not expose sampling or roots.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx

from ..constants import ERROR_DETAIL_CHARS, HTTP_BAD_REQUEST
from ..process import hidden_subprocess_kwargs

#: Seconds to wait for a response before giving up on a request.
DEFAULT_TIMEOUT = 60.0
#: Seconds allowed for a server to shut down cleanly before it is killed.
SHUTDOWN_GRACE = 5.0
#: Largest single line accepted from a server, as a guard against a runaway child.
MAX_LINE_BYTES = 8 * 1024 * 1024


class MCPTransportError(Exception):
    """Raised when the link to a server fails."""


class Transport:
    """Common interface for the two link types."""

    async def start(self) -> None:
        raise NotImplementedError

    async def request(self, payload: dict[str, Any], timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
        raise NotImplementedError

    async def notify(self, payload: dict[str, Any]) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError

    @property
    def alive(self) -> bool:
        return True


class StdioTransport(Transport):
    """Runs an MCP server as a child process and talks to it over pipes."""

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ):
        self.command = command
        self.args = args or []
        self.env = env or {}
        self.cwd = cwd
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._stderr_task: asyncio.Task | None = None
        self.stderr_tail: list[str] = []

    async def start(self) -> None:
        environment = {**os.environ, **self.env}
        try:
            self._process = await asyncio.create_subprocess_exec(
                self.command,
                *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
                cwd=self.cwd,
                limit=MAX_LINE_BYTES,
                **hidden_subprocess_kwargs(),
            )
        except (OSError, ValueError) as error:
            raise MCPTransportError(f"cannot start '{self.command}': {error}") from error
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        """Keep the child's stderr from filling its pipe, and retain the tail.

        A server that blocks writing to a full stderr pipe looks exactly like
        a hung server, so this has to run for the whole session.
        """
        assert self._process is not None and self._process.stderr is not None
        try:
            async for raw in self._process.stderr:
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    self.stderr_tail.append(line)
                    del self.stderr_tail[:-20]
        except (asyncio.CancelledError, ValueError):
            return

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def _write(self, payload: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise MCPTransportError("server is not running")
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        self._process.stdin.write(line.encode("utf-8"))
        await self._process.stdin.drain()

    async def _read_response(self, request_id: Any, timeout: float) -> dict[str, Any]:
        """Read until the reply with the matching id arrives."""
        assert self._process is not None and self._process.stdout is not None
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise MCPTransportError(f"timed out waiting for response to {request_id}")
            try:
                raw = await asyncio.wait_for(self._process.stdout.readline(), timeout=remaining)
            except asyncio.TimeoutError as error:
                raise MCPTransportError(f"timed out waiting for response to {request_id}") from error
            if not raw:
                tail = "; ".join(self.stderr_tail[-3:])
                raise MCPTransportError(f"server closed the connection. stderr: {tail}")
            try:
                message = json.loads(raw.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue  # servers sometimes print banners to stdout
            if not isinstance(message, dict):
                continue
            if message.get("id") == request_id:
                return message
            # Anything else is a notification or a request we do not serve.

    async def request(self, payload: dict[str, Any], timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
        async with self._lock:
            await self._write(payload)
            return await self._read_response(payload.get("id"), timeout)

    async def notify(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            await self._write(payload)

    async def close(self) -> None:
        if self._stderr_task is not None:
            self._stderr_task.cancel()
        if self._process is None:
            return
        if self._process.returncode is None:
            try:
                if self._process.stdin is not None:
                    self._process.stdin.close()
                await asyncio.wait_for(self._process.wait(), timeout=SHUTDOWN_GRACE)
            except (asyncio.TimeoutError, ProcessLookupError, ValueError):
                self._process.kill()
                await self._process.wait()
        self._process = None


class HttpTransport(Transport):
    """Talks to a hosted MCP server over streamable HTTP."""

    def __init__(self, url: str, *, headers: dict[str, str] | None = None, timeout: float = DEFAULT_TIMEOUT):
        self.url = url
        self.headers = headers or {}
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._session_id: str | None = None

    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout))

    def _request_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self.headers,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    async def request(self, payload: dict[str, Any], timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
        if self._client is None:
            raise MCPTransportError("transport not started")
        try:
            response = await self._client.post(
                self.url, json=payload, headers=self._request_headers(), timeout=timeout
            )
        except httpx.HTTPError as error:
            raise MCPTransportError(f"{self.url}: {error}") from error

        session = response.headers.get("Mcp-Session-Id")
        if session:
            self._session_id = session
        if response.status_code >= HTTP_BAD_REQUEST:
            detail = response.text[:ERROR_DETAIL_CHARS]
            raise MCPTransportError(f"{self.url}: HTTP {response.status_code} {detail}")

        content_type = response.headers.get("Content-Type", "")
        if "text/event-stream" in content_type:
            return _first_sse_message(response.text, payload.get("id"))
        try:
            message = response.json()
        except json.JSONDecodeError as error:
            raise MCPTransportError(f"{self.url}: response was not JSON") from error
        if not isinstance(message, dict):
            raise MCPTransportError(f"{self.url}: unexpected response shape")
        return message

    async def notify(self, payload: dict[str, Any]) -> None:
        if self._client is None:
            raise MCPTransportError("transport not started")
        try:
            await self._client.post(self.url, json=payload, headers=self._request_headers())
        except httpx.HTTPError as error:
            raise MCPTransportError(f"{self.url}: {error}") from error

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _first_sse_message(body: str, request_id: Any) -> dict[str, Any]:
    """Pull the reply with the matching id out of an SSE response body."""
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            message = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict) and message.get("id") == request_id:
            return message
    raise MCPTransportError("SSE response contained no matching reply")
