"""The desktop graphical interface.

A local aiohttp server plus a WebView window. The agent, providers, tools,
sessions and everything else are shared with the terminal UI unchanged; only
the presentation layer differs.
"""

from .desktop import launch
from .protocol import PROTOCOL_VERSION, Inbound, Outbound
from .server import GuiServer, serve_forever

__all__ = [
    "PROTOCOL_VERSION",
    "GuiServer",
    "Inbound",
    "Outbound",
    "launch",
    "serve_forever",
]
