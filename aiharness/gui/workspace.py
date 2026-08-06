"""Choosing which project the agent works on.

The working directory is the single most consequential setting in the app: it
bounds what the permission engine will let the agent touch, which skills load,
and which sessions are listed. So it is picked explicitly and shown in the
sidebar at all times, rather than defaulting to wherever the executable
happened to be launched from.

The native folder dialog belongs to the desktop window, which lives on a
different thread from the server. :func:`register_window` bridges that; when
no window is registered — running in a browser via ``aih gui --serve`` — the
frontend falls back to typing a path.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from platformdirs import user_config_dir

RECENTS_FILE = "workspaces.json"
#: How many recent projects are remembered.
MAX_RECENTS = 12

@dataclass
class _WindowHolder:
    """Holds the desktop window, if there is one.

    A module-level object rather than a bare global: the server thread and
    the UI thread both touch this, and a mutable holder keeps the assignment
    in one place instead of scattering ``global`` statements.
    """

    window: object | None = None


_holder = _WindowHolder()


def register_window(window: object) -> None:
    """Give the server a handle on the desktop window."""
    _holder.window = window


def window_handle() -> object | None:
    """Return the live desktop window, if the UI is hosting one."""
    return _holder.window


def reveal_path(path: Path) -> None:
    """Open a file or folder in the OS file manager (select file when possible)."""
    target = path.expanduser().resolve()
    if not target.exists():
        raise FileNotFoundError(str(target))
    if sys.platform == "win32":
        if target.is_file():
            subprocess.Popen(["explorer", "/select,", str(target)])  # noqa: S603
        else:
            os.startfile(str(target))  # type: ignore[attr-defined]
        return
    if sys.platform == "darwin":
        if target.is_file():
            subprocess.run(["open", "-R", str(target)], check=False)  # noqa: S603
        else:
            subprocess.run(["open", str(target)], check=False)  # noqa: S603
        return
    subprocess.run(  # noqa: S603
        ["xdg-open", str(target if target.is_dir() else target.parent)],
        check=False,
    )


def open_path_default(path: Path) -> None:
    """Open a file with the OS default application (preview for images)."""
    target = path.expanduser().resolve()
    if not target.is_file():
        reveal_path(target)
        return
    if sys.platform == "win32":
        os.startfile(str(target))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.run(["open", str(target)], check=False)  # noqa: S603
    else:
        subprocess.run(["xdg-open", str(target)], check=False)  # noqa: S603


def native_folder_dialog(start: Path | None = None) -> str | None:
    """Open the OS folder picker, if a desktop window is available.

    Returns:
      The chosen path, or ``None`` when the user cancelled or there is no
      window to host the dialog.
    """
    if _holder.window is None:
        return None
    try:
        import webview

        chosen = _holder.window.create_file_dialog(
            webview.FOLDER_DIALOG, directory=str(start or Path.home())
        )
    except Exception:  # noqa: BLE001 - a missing dialog must not crash the app
        return None
    if not chosen:
        return None
    return chosen[0] if isinstance(chosen, (list, tuple)) else str(chosen)


def recents_path() -> Path:
    override = os.environ.get("AIH_WORKSPACES_FILE")
    if override:
        return Path(override).expanduser()
    return Path(user_config_dir("aiharness", appauthor=False)) / RECENTS_FILE


@dataclass
class RecentWorkspaces:
    """The most recently opened project directories, newest first."""

    paths: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path | None = None) -> RecentWorkspaces:
        target = path or recents_path()
        if not target.is_file():
            return cls()
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        entries = payload.get("paths") if isinstance(payload, dict) else None
        return cls(paths=[str(p) for p in (entries or [])])

    def save(self, path: Path | None = None) -> None:
        target = path or recents_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"paths": self.paths}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def remember(self, directory: Path) -> None:
        """Move a directory to the front of the list."""
        entry = str(directory)
        self.paths = [entry] + [p for p in self.paths if p != entry]
        del self.paths[MAX_RECENTS:]

    def existing(self) -> list[str]:
        """Recents that still exist on disk.

        A list full of deleted folders is worse than no list: every entry is
        a click that fails.
        """
        return [p for p in self.paths if Path(p).is_dir()]
