"""Entry point for ``python -m aiharness`` and the packaged executable.

Kept minimal on purpose: PyInstaller uses this as the bundle's script, and
anything expensive here is paid on every launch.
"""

from __future__ import annotations

import sys


class _NullWriter:
    """Absorbs output when there is no console to write to."""

    def write(self, _text: str) -> int:
        return 0

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False


def _ensure_streams() -> None:
    """Give the process usable stdout and stderr.

    A windowed PyInstaller build has no console, and sets both streams to
    ``None``. Any ``print`` then raises, which turns a cosmetic log line into
    a crash on startup.
    """
    if sys.stdout is None:
        sys.stdout = _NullWriter()  # type: ignore[assignment]
    if sys.stderr is None:
        sys.stderr = _NullWriter()  # type: ignore[assignment]


def _force_utf8_output() -> None:
    """Make stdout and stderr accept non-ASCII on a legacy Windows console.

    A Chinese Windows install defaults the console to GBK, which cannot
    encode the box-drawing characters the charts use or even an em-dash.
    Without this the first non-ASCII byte raises UnicodeEncodeError and the
    program dies for reasons that have nothing to do with the user's request.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover - depends on the console
            continue


def main() -> int:
    """Run the CLI, translating an interrupt into a clean exit.

    The import is absolute, not relative: PyInstaller runs this file as a
    top-level script with no parent package, so ``from .cli import`` fails in
    the packaged build even though it works under ``python -m aiharness``.
    """
    _ensure_streams()
    _force_utf8_output()
    from aiharness.cli import main as run

    try:
        return run()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
