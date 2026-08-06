"""Choosing a shell, and reading what it says back.

Both bugs here only appeared in the packaged app started from Explorer, and
both produced output that looked like the agent had gone wrong rather than
the harness: every command failed with an unreadable wall of characters.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys

import pytest

from aiharness.tools import shell

WSL_ALIAS = r"C:\Users\someone\AppData\Local\Microsoft\WindowsApps\bash.exe"
WSL_LAUNCHER = r"C:\Windows\System32\bash.exe"
GIT_BASH = r"C:\Program Files\Git\usr\bin\bash.exe"


# -- picking the shell -----------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PATH shims")
@pytest.mark.parametrize("path", [WSL_ALIAS, WSL_LAUNCHER])
def test_wsl_shims_are_not_treated_as_shells(path):
    """``bash.exe`` on the default PATH is WSL, not a shell.

    Windows ships an app-execution alias and a launcher, both named
    ``bash.exe`` and both ahead of Git on PATH. On a machine with no distro
    installed they answer every command with "please install a distribution".
    """
    assert shell._is_usable_bash(path) is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PATH shims")
def test_a_real_git_bash_is_accepted():
    assert shell._is_usable_bash(GIT_BASH) is True


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shell discovery")
def test_git_bash_is_preferred_over_whatever_is_on_the_path(monkeypatch):
    """Install locations are checked first, because the PATH misleads here."""
    monkeypatch.setattr(shell.shutil, "which", lambda _name: WSL_ALIAS)
    executable, prefix, dialect = shell.find_shell()
    assert "WindowsApps" not in executable
    assert prefix == ["-lc"] and dialect == "posix"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shell discovery")
def test_with_no_usable_bash_it_falls_back_to_cmd(monkeypatch):
    """cmd.exe is a poor shell but an honest one; a WSL shim is neither."""
    monkeypatch.setattr(shell, "WINDOWS_BASH_FALLBACKS", ())
    monkeypatch.setattr(shell.shutil, "which", lambda _name: WSL_ALIAS)
    monkeypatch.delenv("AIH_SHELL", raising=False)
    executable, prefix, dialect = shell.find_shell()
    assert dialect == "cmd" and prefix == ["/c"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shell discovery")
def test_an_explicit_override_wins(monkeypatch, tmp_path):
    chosen = tmp_path / "my-bash.exe"
    chosen.write_text("", encoding="utf-8")
    monkeypatch.setenv("AIH_SHELL", str(chosen))
    assert shell.find_shell()[0] == str(chosen)


# -- reading the output ----------------------------------------------------


def test_utf8_output_survives():
    assert shell.decode_output("hello 世界".encode()) == "hello 世界"


def test_utf16_output_is_not_turned_into_mojibake():
    """WSL answers in UTF-16. Read as UTF-8 it became unreadable garbage.

    The message mattered: it was the one explaining that no distribution was
    installed, which is the whole diagnosis.
    """
    message = "适用于 Linux 的 Windows 子系统没有已安装的分发版。"
    assert shell.decode_output(message.encode("utf-16")) == message


def test_console_codepage_output_survives():
    """Native Windows tools emit the console codepage, not UTF-8."""
    assert shell.decode_output("目录不存在".encode("gbk")) == "目录不存在"


def test_empty_output_is_empty():
    assert shell.decode_output(b"") == ""


def test_undecodable_bytes_still_return_something():
    """Losing the output entirely is worse than losing a few characters."""
    assert shell.decode_output(b"\xff\xfe\x00ok\x81\x40") != ""


@pytest.mark.asyncio
async def test_communicate_or_cancel_aborts_when_cancel_set():
    cancel = asyncio.Event()
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(30)",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    cancel.set()
    with pytest.raises(asyncio.CancelledError):
        await shell._communicate_or_cancel(proc, timeout=30.0, cancel=cancel)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    await proc.wait()
