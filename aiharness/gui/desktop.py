"""The desktop window.

pywebview insists on owning the main thread, and aiohttp wants an event loop,
so the server runs in a background thread with its own loop and the window
runs in front. The window closing tears the loop down.

On Windows the renderer is Edge WebView2, which is Chromium — the same engine
Electron bundles, except Windows already has a copy. Same rendering, ~15 MB
instead of ~150 MB, and one process instead of two runtimes.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from ..config.schema import Config
from ..constants import APP_NAME
from .server import GuiServer

#: Opening size. Big enough for the sidebar, the conversation and the
#: context panel side by side without immediately needing a resize.
WINDOW_WIDTH = 1360
WINDOW_HEIGHT = 900
MIN_WIDTH = 900
MIN_HEIGHT = 600
#: Seconds to wait for the server thread to bind before giving up.
SERVER_START_TIMEOUT = 15.0


class ServerThread:
    """Runs the aiohttp server on its own event loop, in its own thread."""

    def __init__(self, config: Config, workspace: Path):
        self.server = GuiServer(config, workspace)
        self.url = ""
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None

    def start(self) -> str:
        """Start the server and return the URL to open.

        Raises:
          RuntimeError: If the server did not come up in time.
        """
        self._thread = threading.Thread(target=self._run, name="aih-gui-server", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=SERVER_START_TIMEOUT):
            raise RuntimeError("the UI server did not start in time")
        if self._error is not None:
            raise RuntimeError(f"the UI server failed to start: {self._error}")
        return self.url

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            self.url = loop.run_until_complete(self.server.start())
        except BaseException as error:  # noqa: BLE001 - reported to the caller
            self._error = error
            self._ready.set()
            return
        self._ready.set()
        loop.run_forever()

    def stop(self) -> None:
        """Shut the server down and stop its loop."""
        if self._loop is None:
            return
        future = asyncio.run_coroutine_threadsafe(self.server.stop(), self._loop)
        try:
            future.result(timeout=SERVER_START_TIMEOUT)
        except Exception:  # noqa: BLE001 - shutdown must not raise
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)


def launch(config: Config, workspace: Path, *, debug: bool = False) -> int:
    """Open the desktop window.

    Args:
      config: The loaded configuration. May be empty; the window opens on the
        settings panel when nothing is configured yet.
      workspace: The agent's working directory.
      debug: Open developer tools alongside the window.

    Returns:
      A process exit code.
    """
    try:
        import webview
    except ImportError:
        print(
            "The desktop window needs pywebview. Install it with:\n"
            '    pip install "aiharness[gui]"'
        )
        return 1

    thread = ServerThread(config, workspace)
    try:
        url = thread.start()
    except RuntimeError as error:
        print(str(error))
        return 1

    window = webview.create_window(
        APP_NAME,
        url,
        width=WINDOW_WIDTH,
        height=WINDOW_HEIGHT,
        min_size=(MIN_WIDTH, MIN_HEIGHT),
        background_color="#1c1917",
        text_select=True,
    )
    # The server thread needs this to open native dialogs.
    from .workspace import register_window

    register_window(window)
    window.events.closed += thread.stop
    tray_holder: list = [None]
    hotkey_holder: list = [None]

    def _trigger_screenshot() -> None:
        from .screenshot_service import run_global_screenshot

        run_global_screenshot(thread, tray=tray_holder[0])

    tray = _attach_tray(window, on_screenshot=_trigger_screenshot)
    tray_holder[0] = tray
    from .hotkey import start_screenshot_hotkey

    hotkey_holder[0] = start_screenshot_hotkey(_trigger_screenshot)

    try:
        webview.start(debug=debug)
    finally:
        if hotkey_holder[0] is not None:
            hotkey_holder[0].stop()
        if tray is not None:
            tray.stop()
        thread.stop()
    return 0


def _attach_tray(window, *, on_screenshot=None):
    """Make the window's X minimise to the tray instead of quitting.

    Returns:
      The live tray, or None when the platform has no usable tray — in which
      case the close button keeps its ordinary meaning. Trapping the window
      with no way to restore it would be far worse than quitting too eagerly.
    """
    from . import tray as tray_module

    quitting = threading.Event()

    def _really_quit() -> None:
        quitting.set()
        try:
            window.destroy()
        except Exception:  # noqa: BLE001 - the window may already be gone
            pass

    icon = tray_module.start(
        on_show=window.show,
        on_quit=_really_quit,
        on_screenshot=on_screenshot,
    )
    if icon is None:
        return None

    shown_hint = threading.Event()

    def _on_closing() -> bool:
        """Cancel the close and hide, unless the tray asked us to quit."""
        if quitting.is_set():
            return True
        window.hide()
        if not shown_hint.is_set():
            shown_hint.set()
            icon.notify(
                f"{APP_NAME} 仍在运行",
                "已最小化到托盘；进行中的对话、心跳与子任务会继续。"
                "右键托盘图标 → 退出程序，才会真正结束。"
                "全局截屏：Ctrl+Shift+S。",
            )
        return False

    window.events.closing += _on_closing
    return icon
