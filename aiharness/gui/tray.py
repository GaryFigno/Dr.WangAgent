"""System tray icon, so closing the window does not kill the agent.

Closing a window and quitting a program are different intentions, and for
this program they are very different: a heartbeat may be iterating, a turn
may be mid-flight, a scheduled task may be waiting for its hour. Losing all
of that to a stray click on the X is not acceptable, so the X hides the
window and only the tray menu's *exit* really quits.

The dangerous failure mode is the opposite one: a window that refuses to
close and no tray icon to restore it, which is a program the user cannot get
rid of without the task manager. Everything here is therefore written so
that *any* failure — pystray missing, the icon thread dying, the image
unreadable — falls back to a window that closes normally. :func:`start`
returns ``None`` on failure and the caller treats that as "no tray".
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from ..constants import APP_NAME, APP_SLUG

#: Tray icons are small; anything larger is wasted and slows the first paint.
TRAY_ICON_SIZE = 64
#: Seconds to wait for the icon to appear before deciding the tray is broken.
TRAY_READY_TIMEOUT = 5.0
#: Drawn when the packaged icon cannot be read, so the tray is never blank.
FALLBACK_BACKGROUND = (28, 25, 23)
FALLBACK_FOREGROUND = (232, 168, 92)


def icon_path() -> Path:
    """Locate the tray image, in the source tree or in a frozen bundle."""
    import sys

    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    for candidate in (
        root / "assets" / f"icon-{TRAY_ICON_SIZE}.png",
        root / "assets" / "icon-128.png",
        root / "assets" / "icon-256.png",
        root / "assets" / "icon.ico",
    ):
        if candidate.is_file():
            return candidate
    return Path()


def _load_image():
    """Return a PIL image for the tray, drawing a placeholder if need be."""
    from PIL import Image, ImageDraw

    path = icon_path()
    if path:
        try:
            return Image.open(path).convert("RGBA")
        except OSError:
            pass
    image = Image.new("RGBA", (TRAY_ICON_SIZE, TRAY_ICON_SIZE), FALLBACK_BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.ellipse((8, 8, TRAY_ICON_SIZE - 8, TRAY_ICON_SIZE - 8), fill=FALLBACK_FOREGROUND)
    return image


class Tray:
    """A tray icon whose menu can show the window or quit for real."""

    def __init__(
        self,
        on_show: Callable[[], None],
        on_quit: Callable[[], None],
        on_screenshot: Callable[[], None] | None = None,
    ):
        self._on_show = on_show
        self._on_quit = on_quit
        self._on_screenshot = on_screenshot
        self._icon = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def start(self) -> bool:
        """Show the icon. Returns False when the tray is unavailable."""
        try:
            import pystray
        except ImportError:
            return False

        # Building the menu is inside the guard on purpose: pystray imports
        # cleanly on desktops where it cannot actually run, and a tray we
        # cannot build must degrade to "no tray", never to a failed launch.
        try:
            items = [
                pystray.MenuItem("显示主窗口", self._show, default=True),
                pystray.MenuItem("截屏 (Ctrl+Shift+S)", self._screenshot),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出程序", self._quit),
            ]
            menu = pystray.Menu(*items)
            self._icon = pystray.Icon(APP_SLUG, _load_image(), APP_NAME, menu)
        except Exception:  # noqa: BLE001 - no tray is survivable, a crash is not
            return False

        self._thread = threading.Thread(target=self._run, name="aih-tray", daemon=True)
        self._thread.start()
        return self._ready.wait(timeout=TRAY_READY_TIMEOUT)

    def _run(self) -> None:
        def _setup(icon) -> None:
            icon.visible = True
            self._ready.set()

        try:
            self._icon.run(setup=_setup)
        except Exception:  # noqa: BLE001 - reported through the ready flag
            self._ready.set()

    def _show(self, *_: object) -> None:
        self._on_show()

    def _screenshot(self, *_: object) -> None:
        if self._on_screenshot is not None:
            self._on_screenshot()

    def _quit(self, *_: object) -> None:
        """Tear the icon down first, then let the caller close the window."""
        self.stop()
        self._on_quit()

    def notify(self, title: str, text: str) -> None:
        """Best-effort balloon. Silence is fine; a crash here is not."""
        if self._icon is None:
            return
        try:
            self._icon.notify(text, title)
        except Exception:  # noqa: BLE001 - unsupported on some desktops
            pass

    def stop(self) -> None:
        if self._icon is None:
            return
        try:
            self._icon.stop()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            pass
        self._icon = None


def start(
    on_show: Callable[[], None],
    on_quit: Callable[[], None],
    on_screenshot: Callable[[], None] | None = None,
) -> Tray | None:
    """Create and show a tray icon, or return None if that is not possible."""
    tray = Tray(on_show, on_quit, on_screenshot=on_screenshot)
    if not tray.start():
        tray.stop()
        return None
    return tray
