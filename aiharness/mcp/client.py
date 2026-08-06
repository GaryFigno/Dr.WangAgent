"""A minimal MCP client.

Implements the part of the Model Context Protocol this harness actually
needs: connect, discover tools, call them. Prompts, resources, sampling and
roots are deliberately not implemented — they would add surface area without
changing what the agent can do.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

from .transport import DEFAULT_TIMEOUT, Transport

#: The protocol revision this client speaks.
PROTOCOL_VERSION = "2025-06-18"
#: How this client identifies itself during the handshake.
CLIENT_NAME = "aiharness"
CLIENT_VERSION = "0.1.0"
#: Seconds allowed for the initial handshake, which may involve a cold start.
HANDSHAKE_TIMEOUT = 30.0
#: Characters of a tool result kept when the server returns something huge.
MAX_RESULT_CHARS = 60000


class MCPError(Exception):
    """Raised when a server returns a JSON-RPC error."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


@dataclass
class MCPTool:
    """A tool as described by a server's ``tools/list``."""

    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    #: Server-declared hints; used to decide whether a call needs approval.
    annotations: dict[str, Any] = field(default_factory=dict)

    @property
    def read_only(self) -> bool:
        return bool(self.annotations.get("readOnlyHint"))

    @property
    def destructive(self) -> bool:
        # Absent the hint, assume a non-read-only tool can destroy something.
        if self.read_only:
            return False
        return bool(self.annotations.get("destructiveHint", True))


class MCPClient:
    """One connection to one MCP server."""

    def __init__(self, server_id: str, transport: Transport):
        self.server_id = server_id
        self.transport = transport
        self.server_info: dict[str, Any] = {}
        self.capabilities: dict[str, Any] = {}
        self._ids = itertools.count(1)
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected and self.transport.alive

    # -- JSON-RPC ---------------------------------------------------------

    async def _call(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = DEFAULT_TIMEOUT
    ) -> dict[str, Any]:
        payload = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": method,
            "params": params or {},
        }
        message = await self.transport.request(payload, timeout=timeout)
        if "error" in message:
            error = message["error"] or {}
            raise MCPError(
                f"[{self.server_id}] {method}: {error.get('message', error)}",
                code=error.get("code"),
            )
        result = message.get("result")
        return result if isinstance(result, dict) else {}

    # -- lifecycle --------------------------------------------------------

    async def connect(self) -> None:
        """Start the transport and perform the MCP handshake.

        Raises:
          MCPTransportError: If the server cannot be reached or started.
          MCPError: If the server rejects the handshake.
        """
        await self.transport.start()
        result = await self._call(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            },
            timeout=HANDSHAKE_TIMEOUT,
        )
        self.server_info = result.get("serverInfo") or {}
        self.capabilities = result.get("capabilities") or {}
        await self.transport.notify({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._connected = True

    async def close(self) -> None:
        self._connected = False
        await self.transport.close()

    # -- tools ------------------------------------------------------------

    async def list_tools(self) -> list[MCPTool]:
        """Fetch the server's tool catalogue, following pagination."""
        if "tools" not in self.capabilities:
            return []
        tools: list[MCPTool] = []
        cursor: str | None = None
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = await self._call("tools/list", params)
            for entry in result.get("tools") or []:
                if not isinstance(entry, dict) or not entry.get("name"):
                    continue
                tools.append(
                    MCPTool(
                        name=str(entry["name"]),
                        description=str(entry.get("description") or ""),
                        input_schema=entry.get("inputSchema") or {"type": "object", "properties": {}},
                        annotations=entry.get("annotations") or {},
                    )
                )
            cursor = result.get("nextCursor")
            if not cursor:
                return tools

    async def call_tool(
        self, name: str, arguments: dict[str, Any], *, timeout: float = DEFAULT_TIMEOUT
    ) -> tuple[str, bool]:
        """Invoke a remote tool.

        Args:
          name: The tool's name as reported by the server.
          arguments: Arguments matching the tool's input schema.
          timeout: Seconds to wait for the call.

        Returns:
          A tuple of (rendered text, is_error).
        """
        result = await self._call(
            "tools/call", {"name": name, "arguments": arguments}, timeout=timeout
        )
        return render_content(result.get("content") or []), bool(result.get("isError"))


def render_content(blocks: Any) -> str:
    """Flatten MCP content blocks into text the model can read.

    Servers may return text, images, audio or embedded resources. Only text
    is usable by a text-only agent loop, so anything else is described rather
    than dropped silently — the model needs to know something came back.
    """
    if isinstance(blocks, str):
        return blocks[:MAX_RESULT_CHARS]
    if not isinstance(blocks, list):
        return str(blocks)[:MAX_RESULT_CHARS]

    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(str(block.get("text", "")))
        elif kind == "resource":
            resource = block.get("resource") or {}
            text = resource.get("text")
            uri = resource.get("uri", "?")
            parts.append(str(text) if text else f"[resource {uri}]")
        elif kind == "resource_link":
            parts.append(f"[resource link {block.get('uri', '?')}]")
        elif kind in ("image", "audio"):
            parts.append(f"[{kind} returned, {block.get('mimeType', 'unknown type')} — not readable here]")
        else:
            parts.append(f"[{kind or 'unknown'} content]")

    text = "\n".join(part for part in parts if part)
    if len(text) > MAX_RESULT_CHARS:
        return text[:MAX_RESULT_CHARS] + f"\n\n[truncated at {MAX_RESULT_CHARS} characters]"
    return text or "(empty result)"
