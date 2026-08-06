"""Global screenshot hotkey (Windows RegisterHotKey).

Ctrl+Shift+S works even when Dr.Wang is not focused. The callback decides
whether to attach+edit (window focused) or copy to the clipboard (elsewhere).
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable

#: Ctrl+Shift+S — same chord the in-app UI documents.
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
VK_S = 0x53
WM_HOTKEY = 0x0312
HOTKEY_ID = 0xA147  # arbitrary app-local id


class GlobalHotkey:
    """Background message pump that owns one registered hotkey."""

    def __init__(self, on_trigger: Callable[[], None]):
        self._on_trigger = on_trigger
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._ok = False

    def start(self) -> bool:
        if sys.platform != "win32":
            return False
        self._thread = threading.Thread(
            target=self._run, name="aih-global-hotkey", daemon=True
        )
        self._thread.start()
        self._ready.wait(timeout=3.0)
        return self._ok

    def stop(self) -> None:
        if sys.platform != "win32" or self._thread is None:
            return
        try:
            import ctypes

            if self._thread_id:
                ctypes.windll.user32.PostThreadMessageW(
                    self._thread_id, 0x0012, 0, 0  # WM_QUIT
                )
        except Exception:  # noqa: BLE001
            pass
        self._thread.join(timeout=2.0)
        self._thread = None

    def _run(self) -> None:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self._thread_id = int(kernel32.GetCurrentThreadId())
        mods = MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT
        if not user32.RegisterHotKey(None, HOTKEY_ID, mods, VK_S):
            self._ok = False
            self._ready.set()
            return
        self._ok = True
        self._ready.set()

        msg = wintypes.MSG()
        try:
            while True:
                got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if got in (0, -1):
                    break
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    threading.Thread(
                        target=self._safe_trigger,
                        name="aih-hotkey-shot",
                        daemon=True,
                    ).start()
                else:
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            user32.UnregisterHotKey(None, HOTKEY_ID)

    def _safe_trigger(self) -> None:
        try:
            self._on_trigger()
        except Exception:  # noqa: BLE001 - never kill the hotkey pump
            pass


def start_screenshot_hotkey(on_trigger: Callable[[], None]) -> GlobalHotkey | None:
    """Register the global chord; return None when unavailable."""
    hotkey = GlobalHotkey(on_trigger)
    if not hotkey.start():
        hotkey.stop()
        return None
    return hotkey
