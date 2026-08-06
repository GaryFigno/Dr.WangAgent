"""Capture the local screen for attaching to a chat turn.

User-facing composer / global-hotkey action (not the agent Desktop tools).
Supports interactive region selection and copying the result to the clipboard.
"""

from __future__ import annotations

import asyncio
import base64
import io
import sys
import time
from dataclasses import dataclass
from typing import Any

from ..constants import ATTACHMENT_MAX_BYTES

#: Brief pause after hiding our window so the compositor paints what is behind.
HIDE_SETTLE_SECONDS = 0.28
#: JPEG quality steps when a PNG would exceed the attachment limit.
_JPEG_QUALITIES = (88, 76, 64, 52)


@dataclass(frozen=True)
class ScreenCapture:
    """One screenshot ready for the composer attach strip or clipboard."""

    mime: str
    data: bytes
    name: str
    width: int
    height: int

    def to_wire(self) -> dict[str, Any]:
        return {
            "mime": self.mime,
            "name": self.name,
            "data": base64.b64encode(self.data).decode("ascii"),
            "width": self.width,
            "height": self.height,
            "open_editor": True,
        }


class CaptureError(RuntimeError):
    """User-facing failure while taking a screenshot."""


class CaptureCancelledError(CaptureError):
    """User dismissed the region selector."""


def _grab_image():
    """Return a PIL Image of the virtual desktop."""
    try:
        from PIL import ImageGrab

        image = ImageGrab.grab(all_screens=True)
        if image is not None:
            return image
    except TypeError:
        try:
            from PIL import ImageGrab

            image = ImageGrab.grab()
            if image is not None:
                return image
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass

    try:
        import pyautogui

        return pyautogui.screenshot()
    except Exception as error:  # noqa: BLE001
        raise CaptureError(
            "无法截屏：需要 Pillow 或 pyautogui，且本机有可用显示器。"
        ) from error


def _encode_under_limit(image) -> tuple[str, bytes]:
    """Encode PNG, falling back to JPEG so the payload fits attachment limits."""
    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGBA")

    png_buf = io.BytesIO()
    image.save(png_buf, format="PNG", optimize=True)
    png_bytes = png_buf.getvalue()
    if len(png_bytes) <= ATTACHMENT_MAX_BYTES:
        return "image/png", png_bytes

    rgb = image.convert("RGB")
    for quality in _JPEG_QUALITIES:
        buf = io.BytesIO()
        rgb.save(buf, format="JPEG", quality=quality, optimize=True)
        data = buf.getvalue()
        if len(data) <= ATTACHMENT_MAX_BYTES:
            return "image/jpeg", data

    scaled = rgb
    for _ in range(6):
        w, h = scaled.size
        scaled = scaled.resize((max(w // 2, 1), max(h // 2, 1)))
        buf = io.BytesIO()
        scaled.save(buf, format="JPEG", quality=70, optimize=True)
        data = buf.getvalue()
        if len(data) <= ATTACHMENT_MAX_BYTES:
            return "image/jpeg", data
    raise CaptureError("截图像素过大，压缩后仍超过附件上限")


def _image_to_capture(image) -> ScreenCapture:
    width, height = image.size
    mime, data = _encode_under_limit(image)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    ext = ".jpg" if mime == "image/jpeg" else ".png"
    return ScreenCapture(
        mime=mime,
        data=data,
        name=f"screenshot-{stamp}{ext}",
        width=width,
        height=height,
    )


def capture_screen_sync(*, interactive: bool = True) -> ScreenCapture:
    """Grab the screen on the calling thread.

    When ``interactive`` is true, the user drags a region (Enter = full screen,
    Esc = cancel).
    """
    image = _grab_image()
    if interactive:
        from .region_select import select_region

        cropped = select_region(image)
        if cropped is None:
            raise CaptureCancelledError("已取消截屏")
        image = cropped
    return _image_to_capture(image)


def copy_image_to_clipboard(image) -> None:
    """Put a PIL image on the system clipboard (best-effort)."""
    if sys.platform == "win32":
        _copy_image_windows(image)
        return
    if sys.platform == "darwin":
        _copy_image_macos(image)
        return
    raise CaptureError("当前系统暂不支持把截屏写入剪贴板")


def copy_capture_to_clipboard(shot: ScreenCapture) -> None:
    """Decode a :class:`ScreenCapture` and place it on the clipboard."""
    from PIL import Image

    image = Image.open(io.BytesIO(shot.data))
    copy_image_to_clipboard(image)


def _copy_image_windows(image) -> None:
    import ctypes

    # CF_DIB wants a BMP without the 14-byte file header.
    buf = io.BytesIO()
    image.convert("RGB").save(buf, "BMP")
    dib = buf.getvalue()[14:]

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    cf_dib = 8
    gmem_moveable = 0x0002

    if not user32.OpenClipboard(None):
        raise CaptureError("无法打开剪贴板")
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(gmem_moveable, len(dib))
        if not handle:
            raise CaptureError("剪贴板内存分配失败")
        locked = kernel32.GlobalLock(handle)
        if not locked:
            kernel32.GlobalFree(handle)
            raise CaptureError("剪贴板内存锁定失败")
        ctypes.memmove(locked, dib, len(dib))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(cf_dib, handle):
            kernel32.GlobalFree(handle)
            raise CaptureError("写入剪贴板失败")
    finally:
        user32.CloseClipboard()


def _copy_image_macos(image) -> None:
    import subprocess
    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
        path = Path(handle.name)
        image.save(handle, format="PNG")
    try:
        script = (
            f'set the clipboard to (read (POSIX file "{path}") as «class PNGf»)'
        )
        completed = subprocess.run(  # noqa: S603
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
        )
        if completed.returncode != 0:
            raise CaptureError("写入剪贴板失败")
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def window_is_visible() -> bool:
    """True when the desktop window is currently shown (not tray-hidden)."""
    from .workspace import window_handle

    window = window_handle()
    if window is None:
        return False
    flagged = getattr(window, "hidden", None)
    if isinstance(flagged, bool):
        return not flagged
    if sys.platform == "win32":
        hwnd = _webview_hwnd(window)
        if hwnd:
            try:
                import ctypes

                return bool(ctypes.windll.user32.IsWindowVisible(int(hwnd)))
            except Exception:  # noqa: BLE001
                return True
    return True


def _set_window_visible(visible: bool) -> bool:
    """Show or hide the pywebview window. Returns True if a window was touched."""
    from .workspace import window_handle

    window = window_handle()
    if window is None:
        return False
    try:
        if visible:
            window.show()
        else:
            window.hide()
        return True
    except Exception:  # noqa: BLE001
        return False


def is_app_foreground() -> bool:
    """True when Dr.Wang's desktop window is the foreground window."""
    if sys.platform != "win32":
        from .workspace import window_handle

        return window_handle() is not None
    try:
        import ctypes

        from .workspace import window_handle

        window = window_handle()
        if window is None:
            return False
        foreground = ctypes.windll.user32.GetForegroundWindow()
        if not foreground:
            return False
        hwnd = _webview_hwnd(window)
        if hwnd and int(hwnd) == int(foreground):
            return True
        # WebView2 often focuses a child HWND; compare process ids.
        pid_fg = ctypes.c_ulong()
        pid_us = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(foreground, ctypes.byref(pid_fg))
        if hwnd:
            ctypes.windll.user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid_us))
            if pid_fg.value and pid_fg.value == pid_us.value:
                return True
        import os

        ctypes.windll.user32.GetWindowThreadProcessId(foreground, ctypes.byref(pid_fg))
        return bool(pid_fg.value == os.getpid())
    except Exception:  # noqa: BLE001
        return False


def _webview_hwnd(window: object) -> int | None:
    """Best-effort native HWND from a pywebview window."""
    for attr in ("native", "hwnd", "handle"):
        value = getattr(window, attr, None)
        if isinstance(value, int) and value:
            return value
    gui = getattr(window, "gui", None)
    if gui is not None:
        for attr in ("hwnd", "handle"):
            value = getattr(gui, attr, None)
            if isinstance(value, int) and value:
                return value
    # pywebview WinForms / Win32 wrappers sometimes expose .Handle
    native = getattr(window, "native", None)
    if native is not None:
        handle = getattr(native, "Handle", None)
        if handle is not None:
            try:
                return int(handle)
            except (TypeError, ValueError):
                pass
        if isinstance(native, dict):
            for key in ("window", "handle", "hwnd"):
                item = native.get(key)
                if item is None:
                    continue
                handle = getattr(item, "Handle", item)
                try:
                    return int(handle)
                except (TypeError, ValueError):
                    continue
    return None


async def capture_screen(
    *, hide_self: bool = True, interactive: bool = True
) -> ScreenCapture:
    """Capture the screen, optionally hiding Dr.Wang and picking a region."""
    hidden = False
    if hide_self:
        hidden = await asyncio.to_thread(_set_window_visible, False)
        if hidden:
            await asyncio.sleep(HIDE_SETTLE_SECONDS)
    try:
        return await asyncio.to_thread(
            capture_screen_sync, interactive=interactive
        )
    finally:
        if hidden:
            await asyncio.to_thread(_set_window_visible, True)
