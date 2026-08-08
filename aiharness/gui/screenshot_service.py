"""Coordinate interactive screenshots from the button, hotkey, or tray.

Rules:
* Always drag-select a region (Enter = full screen, Esc = cancel).
* When Dr.Wang is the foreground window → attach to composer and open editor.
* When invoked from elsewhere → copy to the system clipboard only.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .desktop import ServerThread
    from .tray import Tray

_lock = threading.Lock()
_busy = threading.Event()


def run_global_screenshot(
    server_thread: ServerThread,
    *,
    tray: Tray | None = None,
    force_clipboard: bool | None = None,
) -> None:
    """Entry point for the global hotkey / tray menu (sync, worker thread)."""
    if not _lock.acquire(blocking=False):
        return
    if _busy.is_set():
        _lock.release()
        return
    _busy.set()
    _lock.release()
    try:
        _run(server_thread, tray=tray, force_clipboard=force_clipboard)
    finally:
        with _lock:
            _busy.clear()


def _run(
    server_thread: ServerThread,
    *,
    tray: Tray | None,
    force_clipboard: bool | None,
) -> None:
    from .capture import (
        CaptureCancelledError,
        CaptureError,
        _set_window_visible,
        capture_screen_sync,
        copy_capture_to_clipboard,
        is_app_foreground,
        window_is_visible,
    )

    to_clipboard = (
        force_clipboard
        if force_clipboard is not None
        else (not is_app_foreground())
    )
    was_visible = window_is_visible()
    if was_visible:
        _set_window_visible(False)
        time.sleep(0.28)

    shot = None
    try:
        shot = capture_screen_sync(interactive=True)
    except CaptureCancelledError:
        return
    except CaptureError as error:
        if tray is not None:
            tray.notify("截屏失败", str(error))
        return
    except Exception as error:  # noqa: BLE001
        if tray is not None:
            tray.notify("截屏失败", str(error))
        return
    finally:
        if was_visible:
            _set_window_visible(True)

    if shot is None:
        return

    if to_clipboard:
        _deliver_clipboard_or_file(shot, tray, reason="主窗口未在前台")
        return

    loop = server_thread._loop
    if loop is None or not loop.is_running():
        _deliver_clipboard_or_file(shot, tray, reason="界面未连接")
        return

    future = asyncio.run_coroutine_threadsafe(
        server_thread.server.push_screenshot_to_clients(shot), loop
    )
    try:
        delivered = future.result(timeout=10)
    except Exception:  # noqa: BLE001
        delivered = False
    if not delivered:
        _deliver_clipboard_or_file(shot, tray, reason="无打开的会话")


def _deliver_clipboard_or_file(
    shot,
    tray: Tray | None,
    *,
    reason: str,
) -> None:
    """Clipboard first; if locked/busy, save a file so the shot is not lost."""
    from .capture import CaptureError, copy_capture_to_clipboard, save_capture_fallback

    try:
        copy_capture_to_clipboard(shot)
    except CaptureError as error:
        try:
            path = save_capture_fallback(shot)
        except Exception:  # noqa: BLE001
            if tray is not None:
                tray.notify("截屏失败", str(error))
            return
        if tray is not None:
            tray.notify(
                "截屏已保存",
                f"剪贴板不可用（{error}），已存到 {path}",
            )
        return
    if tray is not None:
        tray.notify(
            "截屏已复制",
            f"{shot.width}×{shot.height} · {reason} · 已在剪贴板，可直接粘贴",
        )
