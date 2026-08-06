"""MCP client, transport and tool-proxy behaviour."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from aiharness.config.schema import Config, MCPServerConfig
from aiharness.mcp.client import MCPClient, render_content
from aiharness.mcp.manager import MCPManager, tool_name_for
from aiharness.mcp.transport import MCPTransportError, StdioTransport
from aiharness.permissions import Decision, PermissionEngine
from aiharness.providers.router import Router
from aiharness.tools.base import ToolContext
from aiharness.toolset import build_registry

SERVER_SCRIPT = str(Path(__file__).parent / "fake_mcp_server.py")


def server_config(server_id: str = "fake", **overrides) -> MCPServerConfig:
    return MCPServerConfig(
        id=server_id, command=sys.executable, args=[SERVER_SCRIPT], **overrides
    )


@pytest.fixture
async def client():
    handle = MCPClient("fake", StdioTransport(sys.executable, [SERVER_SCRIPT]))
    await handle.connect()
    yield handle
    await handle.close()


# -- protocol --------------------------------------------------------------


async def test_handshake_reports_server_identity(client):
    assert client.connected
    assert client.server_info["name"] == "fake-mcp"
    assert client.server_info["version"] == "1.2.3"
    assert "tools" in client.capabilities


async def test_tool_listing_follows_pagination(client):
    tools = await client.list_tools()
    names = [tool.name for tool in tools]
    assert names == ["echo", "explode", "wipe"]


async def test_annotations_drive_the_read_only_flag(client):
    tools = {tool.name: tool for tool in await client.list_tools()}
    assert tools["echo"].read_only is True
    assert tools["echo"].destructive is False
    assert tools["wipe"].destructive is True
    # No hint at all: assume it can destroy something.
    assert tools["explode"].destructive is True


async def test_call_returns_text(client):
    text, is_error = await client.call_tool("echo", {"text": "hello mcp"})
    assert text == "hello mcp"
    assert is_error is False


async def test_tool_reported_errors_are_flagged_not_raised(client):
    text, is_error = await client.call_tool("explode", {})
    assert is_error is True
    assert "went wrong" in text


async def test_protocol_errors_raise(client):
    from aiharness.mcp.client import MCPError

    with pytest.raises(MCPError):
        await client.call_tool("nonexistent", {})


async def test_missing_executable_is_reported_clearly():
    handle = MCPClient("ghost", StdioTransport("definitely-not-a-real-binary-xyz"))
    with pytest.raises(MCPTransportError):
        await handle.connect()


# -- content rendering -----------------------------------------------------


def test_render_content_joins_text_blocks():
    blocks = [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}]
    assert render_content(blocks) == "one\ntwo"


def test_render_content_describes_unreadable_blocks():
    rendered = render_content([{"type": "image", "mimeType": "image/png"}])
    assert "image" in rendered
    assert "not readable" in rendered


def test_render_content_unwraps_embedded_resources():
    blocks = [{"type": "resource", "resource": {"uri": "file:///a", "text": "contents"}}]
    assert render_content(blocks) == "contents"


def test_render_content_handles_an_empty_result():
    assert render_content([]) == "(empty result)"


# -- the manager -----------------------------------------------------------


async def test_manager_namespaces_tools_by_server():
    manager = MCPManager([server_config("alpha")])
    statuses = await manager.connect_all()
    try:
        assert statuses[0].connected
        assert statuses[0].tool_count == 3
        names = [tool.name for tool in manager.tools]
        assert tool_name_for("alpha", "echo") in names
        assert all(name.startswith("mcp__alpha__") for name in names)
    finally:
        await manager.close()


async def test_manager_applies_allow_and_deny_lists():
    manager = MCPManager([server_config("filtered", tools_deny=["wipe"])])
    await manager.connect_all()
    try:
        names = [tool.spec.name for tool in manager.tools]
        assert "wipe" not in names
        assert "echo" in names
    finally:
        await manager.close()

    manager = MCPManager([server_config("only-echo", tools_allow=["echo"])])
    await manager.connect_all()
    try:
        assert [tool.spec.name for tool in manager.tools] == ["echo"]
    finally:
        await manager.close()


async def test_one_broken_server_does_not_stop_the_others():
    manager = MCPManager(
        [
            MCPServerConfig(id="broken", command="definitely-not-a-real-binary-xyz"),
            server_config("working"),
        ]
    )
    statuses = await manager.connect_all()
    try:
        by_id = {status.id: status for status in statuses}
        assert by_id["broken"].connected is False
        assert by_id["broken"].error
        assert by_id["working"].connected is True
        assert manager.tools  # the healthy server still contributed tools
    finally:
        await manager.close()


async def test_disabled_servers_are_skipped():
    manager = MCPManager([server_config("off", enabled=False)])
    assert await manager.connect_all() == []
    assert manager.tools == []


# -- integration with the tool layer ---------------------------------------


async def test_mcp_tools_run_through_the_registry(config, workspace):
    manager = MCPManager([server_config("srv")])
    await manager.connect_all()
    router = Router(config)
    try:
        registry = build_registry(extra_tools=manager.tools)
        assert tool_name_for("srv", "echo") in registry.names()

        ctx = ToolContext(
            workspace=workspace,
            config=config,
            permissions=PermissionEngine(config.permissions, workspace),
            router=router,
        )
        tool = registry.get(tool_name_for("srv", "echo"))
        result = await tool.guarded_run({"text": "through the registry"}, ctx)
        assert result.content == "through the registry"
        assert not result.is_error
    finally:
        await manager.close()
        await router.aclose()


async def test_mcp_tools_obey_the_permission_engine(config, workspace):
    manager = MCPManager([server_config("srv")])
    await manager.connect_all()
    router = Router(config)
    try:
        permissions = PermissionEngine(config.permissions, workspace)
        permissions.set_mode("ask")
        name = tool_name_for("srv", "wipe")

        # Nothing auto-approves a remote tool: in ask mode it must prompt.
        assert permissions.check(name, {}).decision is Decision.ASK

        ctx = ToolContext(
            workspace=workspace,
            config=config,
            permissions=permissions,
            router=router,
            approve=None,  # no approval channel available
        )
        result = await build_registry(extra_tools=manager.tools).get(name).guarded_run({}, ctx)
        assert result.is_error
        assert "Permission required" in result.content
    finally:
        await manager.close()
        await router.aclose()


async def test_a_deny_rule_can_block_a_whole_server(config, workspace):
    manager = MCPManager([server_config("srv")])
    await manager.connect_all()
    try:
        config.permissions.deny = ["mcp__srv__*"]
        permissions = PermissionEngine(config.permissions, workspace)
        verdict = permissions.check(tool_name_for("srv", "echo"), {"text": "x"})
        assert verdict.decision is Decision.DENY
    finally:
        await manager.close()


# -- configuration ---------------------------------------------------------


def test_config_validation_catches_unusable_servers():
    config = Config(
        mcp_servers=[
            MCPServerConfig(id="neither"),
            MCPServerConfig(id="both", command="x", url="https://example.com/mcp"),
            MCPServerConfig(id="dup", command="a"),
            MCPServerConfig(id="dup", command="b"),
        ]
    )
    problems = " ".join(config.validate())
    assert "needs either a command or a url" in problems
    assert "sets both command and url" in problems
    assert "duplicate MCP server id" in problems


def test_example_config_parses_mcp_section(tmp_path):
    from aiharness.config.loader import EXAMPLE, load_config

    (tmp_path / ".aiharness.yaml").write_text(EXAMPLE, encoding="utf-8")
    config = load_config(tmp_path)
    assert config.mcp_servers == []  # the shipped example ships them commented out
