"""Hidden subprocess kwargs keep Windows consoles off the desktop."""

from __future__ import annotations

import subprocess
import sys

import pytest

from aiharness.process import hidden_subprocess_kwargs


@pytest.mark.skipif(sys.platform != "win32", reason="Windows console flags")
def test_windows_hides_child_consoles():
    kwargs = hidden_subprocess_kwargs()
    assert kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
    assert kwargs["startupinfo"].wShowWindow == subprocess.SW_HIDE


@pytest.mark.skipif(sys.platform == "win32", reason="non-Windows")
def test_other_platforms_need_no_flags():
    assert hidden_subprocess_kwargs() == {}
