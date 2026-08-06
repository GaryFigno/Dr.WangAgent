"""The local server the desktop window talks to.

An aiohttp app on ``127.0.0.1`` with an ephemeral port: static files for the
frontend, and one WebSocket carrying every event and command.

Bound to loopback only, and every connection must present the token minted at
startup. That matters more than it looks: this socket can run shell commands
and edit files, so anything else on the machine that can reach it inherits
the agent's authority.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import socket
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from ..config.schema import Config
from ..constants import APP_NAME
from ..session.attachments import ATTACHMENTS_DIR
from ..session.store import SessionStore
from .bridge import GuiSession
from .commands import dispatch
from .protocol import PROTOCOL_VERSION, Outbound, ProtocolError, message, parse_inbound

#: Only loopback. Never bind this to anything routable.
HOST = "127.0.0.1"
#: Where the frontend lives.
WEB_ROOT = Path(__file__).parent / "web"
#: Seconds to wait for a clean shutdown of an open socket.
SHUTDOWN_GRACE = 3.0


class GuiServer:
    """Serves the frontend and bridges one or more browser connections."""

    def __init__(self, config: Config, workspace: Path):
        self.config = config
        self.workspace = workspace
        self.token = secrets.token_urlsafe(24)
        self.port = 0
        self._runner: web.AppRunner | None = None
        self._sessions: dict[web.WebSocketResponse, GuiSession] = {}

    # -- lifecycle --------------------------------------------------------

    @property
    def url(self) -> str:
        """The address the window should open, token included."""
        return f"http://{HOST}:{self.port}/?token={self.token}"

    def _build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/ws", self._websocket)
        app.router.add_get("/health", self._health)
        app.router.add_get("/", self._index)
        app.router.add_get(
            "/attachment/{session_id}/{filename}", self._attachment
        )
        app.router.add_get("/workspace-file", self._workspace_file)
        if WEB_ROOT.is_dir():
            app.router.add_static("/static", WEB_ROOT, name="static")
        return app

    async def start(self) -> str:
        """Start listening on a free loopback port.

        Returns:
          The URL to open, with the auth token in the query string.
        """
        self._runner = web.AppRunner(self._build_app(), access_log=None)
        await self._runner.setup()
        self.port = _free_port()
        site = web.TCPSite(self._runner, HOST, self.port)
        await site.start()
        return self.url

    async def stop(self) -> None:
        for session in list(self._sessions.values()):
            await session.close()
        self._sessions.clear()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # -- routes -----------------------------------------------------------

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "protocol": PROTOCOL_VERSION})

    async def _index(self, request: web.Request) -> web.StreamResponse:
        index = WEB_ROOT / "index.html"
        if not index.is_file():
            return web.Response(
                text="The frontend is missing from this build.", status=500
            )
        return web.FileResponse(index)

    async def _attachment(self, request: web.Request) -> web.StreamResponse:
        """Serve a session-scoped pasted image to the authenticated UI."""
        if not self._authorised(request):
            return web.Response(status=403, text="bad token")
        session_id = request.match_info["session_id"]
        filename = request.match_info["filename"]
        if not session_id or not filename or "/" in filename or "\\" in filename:
            return web.Response(status=400, text="bad path")
        if ".." in session_id or ".." in filename:
            return web.Response(status=400, text="bad path")
        handle = SessionStore().open(session_id)
        if handle is None:
            return web.Response(status=404, text="session not found")
        path = (handle.directory / ATTACHMENTS_DIR / filename).resolve()
        try:
            path.relative_to((handle.directory / ATTACHMENTS_DIR).resolve())
        except ValueError:
            return web.Response(status=404, text="not found")
        if not path.is_file():
            return web.Response(status=404, text="not found")
        return web.FileResponse(path)

    async def _workspace_file(self, request: web.Request) -> web.StreamResponse:
        """Serve a file under a session's workspace for in-UI image previews."""
        if not self._authorised(request):
            return web.Response(status=403, text="bad token")
        session_id = (request.query.get("session") or "").strip()
        raw = (request.query.get("path") or "").strip()
        if not session_id or not raw:
            return web.Response(status=400, text="session and path required")
        handle = SessionStore().open(session_id)
        if handle is None:
            return web.Response(status=404, text="session not found")
        root = Path(handle.meta.workspace).expanduser().resolve()
        candidate = Path(raw).expanduser()
        path = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            return web.Response(status=403, text="outside workspace")
        if not path.is_file():
            return web.Response(status=404, text="not found")
        return web.FileResponse(path)

    def _authorised(self, request: web.Request) -> bool:
        supplied = request.query.get("token") or request.headers.get("X-Auth-Token", "")
        return secrets.compare_digest(supplied, self.token)

    async def _websocket(self, request: web.Request) -> web.WebSocketResponse:
        if not self._authorised(request):
            return web.Response(status=403, text="bad token")

        socket_response = web.WebSocketResponse(heartbeat=30)
        await socket_response.prepare(request)

        async def send(payload: dict[str, Any]) -> None:
            if not socket_response.closed:
                await socket_response.send_json(payload)

        session = GuiSession(self.config, self.workspace, send)
        self._sessions[socket_response] = session

        try:
            await send(message(Outbound.READY, protocol=PROTOCOL_VERSION, transcript=[]))
            await session.push_all()
            await self._pump(socket_response, session)
        finally:
            self._sessions.pop(socket_response, None)
            await session.close()
        return socket_response

    async def _pump(
        self, socket_response: web.WebSocketResponse, session: GuiSession
    ) -> None:
        """Read commands until the browser goes away."""
        async for raw in socket_response:
            if raw.type is not WSMsgType.TEXT:
                continue
            try:
                command, args = parse_inbound(json.loads(raw.data))
            except (json.JSONDecodeError, ProtocolError) as error:
                await session.push(Outbound.ERROR, message=str(error))
                continue
            await dispatch(session, command, args)

    def any_session(self) -> GuiSession | None:
        """Return one live browser session, if any."""
        for session in self._sessions.values():
            return session
        return None

    async def push_screenshot_to_clients(self, shot) -> bool:
        """Deliver a captured image to every connected UI (opens the editor)."""
        if not self._sessions:
            return False
        payload = shot.to_wire()
        for session in list(self._sessions.values()):
            await session.push(Outbound.SCREENSHOT, **payload)
        return True


def _free_port() -> int:
    """Ask the OS for an unused loopback port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((HOST, 0))
        return probe.getsockname()[1]


async def serve_forever(config: Config, workspace: Path) -> None:
    """Run the server until interrupted. Used by ``aih gui --serve``."""
    server = GuiServer(config, workspace)
    url = await server.start()
    print(f"{APP_NAME} UI: {url}")
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await server.stop()
