"""Connecting to MCP servers and exposing their tools to the agent.

Remote tools become ordinary :class:`~aiharness.tools.base.Tool` instances, so
they go through the same permission engine, the same transcript rendering and
the same subagent restrictions as the built-in ones. Nothing about a tool
being remote should make it easier to run.

Tool names are namespaced ``mcp__<server>__<tool>``, which keeps two servers
offering ``search`` from colliding and lets permission rules target a whole
server at once — ``mcp__github__*``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from ..config.schema import MCPServerConfig
from ..tools.base import Tool, ToolContext, ToolResult
from .client import MCPClient, MCPError, MCPTool
from .transport import HttpTransport, MCPTransportError, StdioTransport

#: Prefix that marks a tool as coming from an MCP server.
TOOL_PREFIX = "mcp__"
#: Seconds allowed for one server to connect before it is given up on.
CONNECT_TIMEOUT = 45.0


def tool_name_for(server_id: str, tool_name: str) -> str:
    """Build the namespaced name the model sees."""
    return f"{TOOL_PREFIX}{server_id}__{tool_name}"


@dataclass
class ServerStatus:
    """What happened when we tried to connect to one server."""

    id: str
    connected: bool = False
    tool_count: int = 0
    error: str = ""
    server_name: str = ""
    version: str = ""
    skipped: list[str] = field(default_factory=list)


class MCPToolProxy(Tool):
    """Presents one remote tool as a local tool."""

    #: Remote tools are available to subagents, but never spawn more agents.
    subagent_safe = True
    bulky = True

    def __init__(self, client: MCPClient, spec: MCPTool, server_id: str, timeout: float):
        self.client = client
        self.spec = spec
        self.server_id = server_id
        self.timeout = timeout
        self.name = tool_name_for(server_id, spec.name)
        self.description = self._build_description()

    def _build_description(self) -> str:
        body = self.spec.description.strip() or f"The '{self.spec.name}' tool."
        note = f"\n\nProvided by the '{self.server_id}' MCP server."
        if self.spec.read_only:
            note += " Declared read-only by the server."
        elif self.spec.destructive:
            note += " May modify or delete data on the far side."
        return body + note

    def schema(self) -> dict[str, Any]:
        schema = dict(self.spec.input_schema or {})
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        return schema

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if not self.client.connected:
            return ToolResult.error(
                f"the '{self.server_id}' MCP server is not connected; try /mcp reconnect"
            )
        ctx.note(f"{self.server_id}: {self.spec.name}")
        try:
            text, is_error = await self.client.call_tool(
                self.spec.name, args, timeout=self.timeout
            )
        except (MCPError, MCPTransportError) as error:
            return ToolResult.error(f"{self.name} failed: {error}")
        except asyncio.TimeoutError:
            return ToolResult.error(f"{self.name} timed out after {self.timeout:.0f}s")

        summary = " ".join(text.split())[:120]
        return ToolResult(
            content=text,
            is_error=is_error,
            summary=f"{self.server_id}/{self.spec.name} — {summary}" if summary else self.name,
            display={"kind": "mcp", "server": self.server_id, "tool": self.spec.name},
        )


class MCPManager:
    """Owns every MCP connection for a session."""

    def __init__(self, servers: list[MCPServerConfig]):
        self.servers = [s for s in servers if s.enabled]
        self.clients: dict[str, MCPClient] = {}
        self.statuses: dict[str, ServerStatus] = {}
        self._tools: list[MCPToolProxy] = []

    @property
    def tools(self) -> list[MCPToolProxy]:
        return list(self._tools)

    def _build_transport(self, config: MCPServerConfig):
        if config.url:
            return HttpTransport(config.url, headers=config.headers, timeout=config.timeout)
        if not config.command:
            raise MCPTransportError(f"server '{config.id}' has neither command nor url")
        return StdioTransport(
            config.command, config.args, env=config.env, cwd=config.cwd or None
        )

    def _accepts(self, config: MCPServerConfig, tool_name: str) -> bool:
        """Apply the server's own allow/deny lists to a discovered tool."""
        if config.tools_deny and tool_name in config.tools_deny:
            return False
        if config.tools_allow:
            return tool_name in config.tools_allow
        return True

    async def connect_one(self, config: MCPServerConfig) -> ServerStatus:
        """Connect to one server and register its tools."""
        status = ServerStatus(id=config.id)
        try:
            client = MCPClient(config.id, self._build_transport(config))
            await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT)
            discovered = await client.list_tools()
        except (MCPError, MCPTransportError) as error:
            status.error = str(error)
            return status
        except asyncio.TimeoutError:
            status.error = f"handshake timed out after {CONNECT_TIMEOUT:.0f}s"
            return status
        except Exception as error:  # noqa: BLE001 - one bad server must not stop the rest
            status.error = f"{type(error).__name__}: {error}"
            return status

        self.clients[config.id] = client
        for spec in discovered:
            if not self._accepts(config, spec.name):
                status.skipped.append(spec.name)
                continue
            self._tools.append(MCPToolProxy(client, spec, config.id, config.timeout))

        status.connected = True
        status.tool_count = len(discovered) - len(status.skipped)
        status.server_name = str(client.server_info.get("name", config.id))
        status.version = str(client.server_info.get("version", ""))
        return status

    async def connect_all(self) -> list[ServerStatus]:
        """Connect to every enabled server, concurrently.

        A server that fails is reported and skipped; it never prevents the
        session from starting.
        """
        if not self.servers:
            return []
        results = await asyncio.gather(
            *(self.connect_one(config) for config in self.servers), return_exceptions=True
        )
        statuses: list[ServerStatus] = []
        for config, outcome in zip(self.servers, results, strict=True):
            if isinstance(outcome, ServerStatus):
                statuses.append(outcome)
            else:
                statuses.append(
                    ServerStatus(id=config.id, error=f"{type(outcome).__name__}: {outcome}")
                )
        self.statuses = {status.id: status for status in statuses}
        return statuses

    async def reconnect(self) -> list[ServerStatus]:
        """Drop every connection and start over."""
        await self.close()
        self._tools.clear()
        self.clients.clear()
        return await self.connect_all()

    async def close(self) -> None:
        for client in self.clients.values():
            try:
                await client.close()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                continue
