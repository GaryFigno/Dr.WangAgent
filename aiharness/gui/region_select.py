"""Fullscreen drag-to-select overlay for interactive screenshots.

Uses tkinter (stdlib) so the snipping UI works without extra GUI deps.
The caller supplies a full-desktop PIL image; the user drags a rectangle;
we return a cropped copy or ``None`` if they cancel.
"""

from __future__ import annotations

import sys
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL import Image

#: Ignore click-jitter smaller than this (image pixels).
MIN_SELECTION = 4


def virtual_screen_origin() -> tuple[int, int]:
    """Top-left of the virtual desktop in OS coordinates."""
    if sys.platform == "win32":
        import ctypes

        user32 = ctypes.windll.user32
        return int(user32.GetSystemMetrics(76)), int(user32.GetSystemMetrics(77))
    return 0, 0


def select_region(background: Image.Image) -> Image.Image | None:
    """Show a snipping overlay and return the cropped region, or None.

    Safe to call from a worker thread: runs its own short-lived tk mainloop.
    """
    try:
        import tkinter as tk
    except ImportError as error:  # pragma: no cover
        raise RuntimeError("截屏选区需要 tkinter") from error

    result: list[Image.Image | None] = [None]
    done = threading.Event()
    origin_x, origin_y = virtual_screen_origin()
    width, height = background.size

    def _run() -> None:
        from PIL import ImageTk

        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.geometry(f"{width}x{height}+{origin_x}+{origin_y}")
        root.configure(bg="#000000", cursor="crosshair")

        display = background
        scale = 1.0
        max_edge = 4096
        if max(width, height) > max_edge:
            scale = max_edge / float(max(width, height))
            display = background.resize(
                (max(int(width * scale), 1), max(int(height * scale), 1))
            )
        photo = ImageTk.PhotoImage(display.convert("RGB"))
        dw, dh = display.size

        canvas = tk.Canvas(
            root, width=dw, height=dh, highlightthickness=0,
            cursor="crosshair", bg="#000000",
        )
        canvas.pack(fill="both", expand=True)
        canvas.create_image(0, 0, anchor="nw", image=photo)
        # Keep a reference so Tk does not GC the bitmap.
        canvas.photo = photo  # type: ignore[attr-defined]

        dim_ids: list[int] = []
        sel_id: list[int | None] = [None]
        hint_id = canvas.create_text(
            dw // 2, 28,
            text="拖动选取范围 · Esc 取消 · Enter 全屏",
            fill="#f0e6d8",
            font=("Segoe UI", 14, "bold"),
        )
        drag = {"x0": 0, "y0": 0, "active": False}

        def _clear_overlay() -> None:
            for item in dim_ids:
                canvas.delete(item)
            dim_ids.clear()
            if sel_id[0] is not None:
                canvas.delete(sel_id[0])
                sel_id[0] = None

        def _paint_selection(x1: int, y1: int, x2: int, y2: int) -> None:
            _clear_overlay()
            left, right = min(x1, x2), max(x1, x2)
            top, bottom = min(y1, y2), max(y1, y2)
            for box in (
                (0, 0, dw, top),
                (0, bottom, dw, dh),
                (0, top, left, bottom),
                (right, top, dw, bottom),
            ):
                dim_ids.append(
                    canvas.create_rectangle(
                        *box, fill="#000000", stipple="gray50", outline="",
                    )
                )
            sel_id[0] = canvas.create_rectangle(
                left, top, right, bottom, outline="#e8a05c", width=2,
            )

        def _to_image(x: int, y: int) -> tuple[int, int]:
            if scale == 1.0:
                return int(x), int(y)
            return int(x / scale), int(y / scale)

        def _finish(x0: int, y0: int, x1: int, y1: int) -> None:
            ix0, iy0 = _to_image(min(x0, x1), min(y0, y1))
            ix1, iy1 = _to_image(max(x0, x1), max(y0, y1))
            ix0 = max(0, min(ix0, width - 1))
            iy0 = max(0, min(iy0, height - 1))
            ix1 = max(ix0 + 1, min(ix1, width))
            iy1 = max(iy0 + 1, min(iy1, height))
            if ix1 - ix0 < MIN_SELECTION or iy1 - iy0 < MIN_SELECTION:
                result[0] = None
            else:
                result[0] = background.crop((ix0, iy0, ix1, iy1))
            root.destroy()

        def _on_press(event: tk.Event) -> None:
            drag["active"] = True
            drag["x0"], drag["y0"] = event.x, event.y
            canvas.itemconfigure(hint_id, state="hidden")
            _paint_selection(event.x, event.y, event.x, event.y)

        def _on_drag(event: tk.Event) -> None:
            if drag["active"]:
                _paint_selection(drag["x0"], drag["y0"], event.x, event.y)

        def _on_release(event: tk.Event) -> None:
            if not drag["active"]:
                return
            drag["active"] = False
            _finish(drag["x0"], drag["y0"], event.x, event.y)

        def _cancel(_: object = None) -> None:
            result[0] = None
            root.destroy()

        def _full(_: object = None) -> None:
            result[0] = background.copy()
            root.destroy()

        # Initial dim so the desktop behind is clearly "armed".
        dim_ids.append(
            canvas.create_rectangle(
                0, 0, dw, dh, fill="#000000", stipple="gray50", outline="",
            )
        )
        canvas.bind("<ButtonPress-1>", _on_press)
        canvas.bind("<B1-Motion>", _on_drag)
        canvas.bind("<ButtonRelease-1>", _on_release)
        root.bind("<Escape>", _cancel)
        root.bind("<Return>", _full)
        root.protocol("WM_DELETE_WINDOW", _cancel)
        root.focus_force()
        try:
            root.mainloop()
        finally:
            done.set()

    if threading.current_thread() is threading.main_thread():
        threading.Thread(target=_run, name="aih-region-select", daemon=True).start()
        done.wait(timeout=600)
    else:
        _run()
    return result[0]
