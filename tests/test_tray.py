"""The tray must never be able to trap the user in an unclosable window."""

from __future__ import annotations

import sys

from aiharness.gui import tray


def test_the_icon_image_always_resolves():
    """A missing asset falls back to a drawn placeholder, not an exception."""
    image = tray._load_image()
    assert image.size[0] > 0


def test_a_missing_pystray_means_no_tray_rather_than_a_crash(monkeypatch):
    """Without a tray the window keeps its ordinary close behaviour.

    Returning None here is what makes :func:`~aiharness.gui.desktop.launch`
    leave the close button alone, so a machine with no working tray can still
    quit the program.
    """
    monkeypatch.setitem(sys.modules, "pystray", None)
    assert tray.start(on_show=lambda: None, on_quit=lambda: None) is None


def test_a_tray_that_never_appears_is_reported_as_unavailable(monkeypatch):
    """An icon thread that dies must not leave the caller believing in it."""

    class DeadIcon:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, setup=None):
            raise RuntimeError("no tray on this desktop")

        def stop(self):
            pass

    monkeypatch.setattr(tray, "TRAY_READY_TIMEOUT", 1.0)
    icon = tray.Tray(on_show=lambda: None, on_quit=lambda: None)
    monkeypatch.setattr(icon, "_icon", None)

    class FakeModule:
        Icon = DeadIcon
        Menu = type("Menu", (), {"SEPARATOR": object()})
        MenuItem = lambda *a, **k: None  # noqa: E731

    monkeypatch.setitem(sys.modules, "pystray", FakeModule)
    # start() reports failure; it does not hang and does not raise.
    assert icon.start() is False


def test_quitting_stops_the_icon_before_closing_the_window():
    """Order matters: a live icon outliving the window is a stuck tray entry."""
    events = []

    class FakeIcon:
        def stop(self):
            events.append("icon stopped")

    icon = tray.Tray(on_show=lambda: None, on_quit=lambda: events.append("window closed"))
    icon._icon = FakeIcon()
    icon._quit()

    assert events == ["icon stopped", "window closed"]


def test_notify_survives_a_desktop_without_balloons():
    class Grumpy:
        def notify(self, *args):
            raise NotImplementedError

        def stop(self):
            pass

    icon = tray.Tray(on_show=lambda: None, on_quit=lambda: None)
    icon._icon = Grumpy()
    icon.notify("t", "x")  # must not raise


# -- what the close button does -------------------------------------------


class FakeEvent:
    """Stands in for pywebview's event slot, which uses ``+=``."""

    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self):
        return [handler() for handler in self.handlers]


class FakeWindow:
    def __init__(self):
        self.events = type("E", (), {"closing": FakeEvent(), "closed": FakeEvent()})()
        self.log = []

    def hide(self):
        self.log.append("hidden")

    def show(self):
        self.log.append("shown")

    def destroy(self):
        self.log.append("destroyed")


def test_closing_the_window_hides_it_instead_of_quitting(monkeypatch):
    """The X must not end a run that a heartbeat may still be driving."""
    from aiharness.gui import desktop

    fake_tray = tray.Tray(on_show=lambda: None, on_quit=lambda: None)
    fake_tray._icon = type("I", (), {"notify": lambda *a: None, "stop": lambda s: None})()
    monkeypatch.setattr(
        tray, "start", lambda on_show, on_quit, on_screenshot=None: fake_tray
    )

    window = FakeWindow()
    assert desktop._attach_tray(window) is fake_tray

    assert window.events.closing.fire() == [False], "the close must be cancelled"
    assert window.log == ["hidden"]


def test_only_the_tray_exit_really_closes_the_window(monkeypatch):
    from aiharness.gui import desktop

    captured = {}

    def fake_start(on_show, on_quit, on_screenshot=None):
        captured["quit"] = on_quit
        icon = tray.Tray(on_show=on_show, on_quit=on_quit, on_screenshot=on_screenshot)
        icon._icon = type("I", (), {"notify": lambda *a: None, "stop": lambda s: None})()
        return icon

    monkeypatch.setattr(tray, "start", fake_start)
    window = FakeWindow()
    desktop._attach_tray(window)

    captured["quit"]()
    assert "destroyed" in window.log
    # And now the close is allowed through rather than cancelled again.
    assert window.events.closing.fire() == [True]


def test_without_a_tray_the_close_button_keeps_its_meaning(monkeypatch):
    """A window that cannot be closed and cannot be restored is unusable."""
    from aiharness.gui import desktop

    monkeypatch.setattr(
        tray, "start", lambda on_show, on_quit, on_screenshot=None: None
    )
    window = FakeWindow()

    assert desktop._attach_tray(window) is None
    assert window.events.closing.handlers == [], "nothing may intercept the close"


def test_the_packaged_icon_is_one_the_spec_actually_ships():
    """The tray looked for sizes the bundle did not contain.

    The result was not an error — it was the drawn placeholder, silently, in
    the packaged build only. So the two lists are compared directly.
    """
    import re
    from pathlib import Path

    spec = Path(__file__).resolve().parents[1] / "packaging" / "aiharness.spec"
    shipped = set(re.findall(r'"assets"\s*/\s*"([^"]+)"', spec.read_text(encoding="utf-8")))
    wanted = set(re.findall(r'"assets"\s*/\s*"([^"]+)"',
                            (Path(tray.__file__)).read_text(encoding="utf-8")))
    assert wanted & shipped, f"tray wants {sorted(wanted)}, spec ships {sorted(shipped)}"
