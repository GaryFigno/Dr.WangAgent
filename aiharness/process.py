"""Subprocess helpers that keep console windows off the desktop.

On Windows, a GUI app that spawns ``bash.exe`` / ``cmd.exe`` / ``git.exe``
without ``CREATE_NO_WINDOW`` flashes a console for every call. The agent may
run dozens of Bash tools in one turn; that looks like the app is broken.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any


def hidden_subprocess_kwargs() -> dict[str, Any]:
    """Kwargs so child consoles stay invisible on Windows.

    Safe to splat into :func:`asyncio.create_subprocess_exec` and
    :func:`subprocess.run`. Non-Windows platforms get an empty dict.
    Child *GUI* programs (editors, installers) still open normally; only the
    console allocated for the tool process itself is suppressed.
    """
    if sys.platform != "win32":
        return {}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": flags,
        "startupinfo": startupinfo,
    }
