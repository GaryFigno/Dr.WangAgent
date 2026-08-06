"""A minimal MCP server over stdio, for testing the client.

Run as a subprocess by the tests. Implements initialize, tools/list (with one
page of pagination) and tools/call, plus deliberate failure modes so error
handling can be exercised.
"""

from __future__ import annotations

import json
import sys

TOOLS_PAGE_ONE = [
    {
        "name": "echo",
        "description": "Echo the supplied text back.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "explode",
        "description": "Always returns an error result.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

TOOLS_PAGE_TWO = [
    {
        "name": "wipe",
        "description": "Pretends to delete everything.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"destructiveHint": True},
    },
]


def _respond(request_id, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}) + "\n")
    sys.stdout.flush()


def _error(request_id, code, message):
    sys.stdout.write(
        json.dumps({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})
        + "\n"
    )
    sys.stdout.flush()


def _handle_call(request_id, params):
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if name == "echo":
        _respond(request_id, {"content": [{"type": "text", "text": arguments.get("text", "")}]})
    elif name == "explode":
        _respond(
            request_id,
            {"content": [{"type": "text", "text": "everything went wrong"}], "isError": True},
        )
    elif name == "wipe":
        _respond(request_id, {"content": [{"type": "text", "text": "wiped"}]})
    else:
        _error(request_id, -32602, f"unknown tool: {name}")


def main() -> None:
    # Servers commonly print a banner to stdout before speaking JSON-RPC;
    # the client must tolerate it.
    sys.stdout.write("fake-mcp-server starting\n")
    sys.stdout.flush()

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = message.get("method")
        request_id = message.get("id")

        if method == "initialize":
            _respond(
                request_id,
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "fake-mcp", "version": "1.2.3"},
                },
            )
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            if (message.get("params") or {}).get("cursor") == "page2":
                _respond(request_id, {"tools": TOOLS_PAGE_TWO})
            else:
                _respond(request_id, {"tools": TOOLS_PAGE_ONE, "nextCursor": "page2"})
        elif method == "tools/call":
            _handle_call(request_id, message.get("params") or {})
        elif request_id is not None:
            _error(request_id, -32601, f"method not found: {method}")


if __name__ == "__main__":
    main()
