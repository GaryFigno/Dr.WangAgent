"""Composer screenshot capture (unit)."""

from __future__ import annotations

from PIL import Image

from aiharness.gui.capture import _encode_under_limit, capture_screen_sync


def test_encode_png_when_small():
    image = Image.new("RGB", (32, 24), color=(200, 40, 40))
    mime, data = _encode_under_limit(image)
    assert mime == "image/png"
    assert data[:8] == b"\x89PNG\r\n\x1a\n"


def test_encode_falls_back_to_jpeg_when_png_over_budget(monkeypatch):
    from aiharness.gui import capture as capture_mod

    real_save = Image.Image.save

    def fat_png_save(self, fp, format=None, **kwargs):  # noqa: A002, ANN001
        if format == "PNG":
            fp.write(b"\x89PNG\r\n\x1a\n" + b"\0" * (capture_mod.ATTACHMENT_MAX_BYTES + 1))
            return None
        return real_save(self, fp, format=format, **kwargs)

    monkeypatch.setattr(Image.Image, "save", fat_png_save)
    image = Image.new("RGB", (64, 48), color=(90, 40, 10))
    mime, data = _encode_under_limit(image)
    assert mime == "image/jpeg"
    assert data[:2] == b"\xff\xd8"
    assert len(data) <= capture_mod.ATTACHMENT_MAX_BYTES


def test_capture_screen_sync_uses_grab(monkeypatch):
    fake = Image.new("RGB", (40, 30), color=(1, 2, 3))
    monkeypatch.setattr("aiharness.gui.capture._grab_image", lambda: fake)
    shot = capture_screen_sync(interactive=False)
    assert shot.width == 40
    assert shot.height == 30
    assert shot.mime == "image/png"
    assert shot.name.startswith("screenshot-")
    wire = shot.to_wire()
    assert wire["data"]
    assert wire["mime"] == "image/png"
    assert wire["open_editor"] is True


def test_capture_screen_sync_interactive_crops(monkeypatch):
    fake = Image.new("RGB", (100, 80), color=(1, 2, 3))
    cropped = Image.new("RGB", (20, 10), color=(9, 8, 7))
    monkeypatch.setattr("aiharness.gui.capture._grab_image", lambda: fake)
    monkeypatch.setattr("aiharness.gui.region_select.select_region", lambda _img: cropped)
    shot = capture_screen_sync(interactive=True)
    assert shot.width == 20
    assert shot.height == 10


def test_capture_screen_sync_interactive_cancel(monkeypatch):
    from aiharness.gui.capture import CaptureCancelledError

    fake = Image.new("RGB", (40, 30), color=(1, 2, 3))
    monkeypatch.setattr("aiharness.gui.capture._grab_image", lambda: fake)
    monkeypatch.setattr("aiharness.gui.region_select.select_region", lambda _img: None)
    try:
        capture_screen_sync(interactive=True)
        raise AssertionError("expected cancel")
    except CaptureCancelledError:
        pass
