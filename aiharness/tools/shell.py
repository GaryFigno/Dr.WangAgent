"""Shell execution.

On Windows this prefers Git Bash when it is on PATH so that the same POSIX
command lines work everywhere; it falls back to cmd.exe otherwise.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import locale
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from ..constants import (
    FIRST_PRINTABLE_CODEPOINT,
    GARBLING_LIMIT,
    SHELL_COMMAND_ECHO_CHARS,
    SHELL_DEFAULT_TIMEOUT,
    SHELL_LABEL_CHARS,
    SHELL_MAX_OUTPUT_CHARS,
    SHELL_MAX_TIMEOUT,
    UTF16_SNIFF_BYTES,
)
from ..process import hidden_subprocess_kwargs
from .base import Tool, ToolContext, ToolResult

#: Git Bash locations, searched *before* the PATH. See :func:`find_shell`.
WINDOWS_BASH_FALLBACKS = (
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files\Git\usr\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
)

#: Directories whose ``bash.exe`` is not a shell we can use.
#:
#: ``WindowsApps`` holds the app-execution alias for WSL, and ``System32``
#: holds WSL's own launcher. Both are named ``bash.exe`` and both sit on the
#: default PATH ahead of Git — so on a machine with no WSL distribution
#: installed, every command died with WSL's "please install a distro" notice
#: instead of running. It only reproduced when the app was started from
#: Explorer, because a shell launched from Git Bash puts Git's directories
#: first and masks the problem.
SHELL_PATH_BLOCKLIST = ("windowsapps", "system32", "sysnative", "syswow64")

#: Byte-order marks and what they mean. Checked before any heuristic.
BOMS = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


def _is_usable_bash(path: str) -> bool:
    """Whether a discovered ``bash.exe`` is a real shell rather than a shim."""
    lowered = Path(path).resolve().as_posix().lower()
    return not any(f"/{blocked}/" in lowered for blocked in SHELL_PATH_BLOCKLIST)


def find_shell() -> tuple[str, list[str], str]:
    """Locate a shell to run commands with.

    Git Bash is looked for at its install locations before the PATH is
    consulted, because on Windows the PATH is actively misleading here.

    Returns:
      A tuple of (executable, argument prefix, dialect), where dialect is
      ``posix`` or ``cmd``.
    """
    if sys.platform != "win32":
        return (os.environ.get("SHELL") or "/bin/sh", ["-lc"], "posix")

    override = os.environ.get("AIH_SHELL")
    if override and Path(override).exists():
        return (override, ["-lc"], "posix")

    for guess in WINDOWS_BASH_FALLBACKS:
        if Path(guess).exists():
            return (guess, ["-lc"], "posix")
    for candidate in ("bash.exe", "bash"):
        found = shutil.which(candidate)
        if found and _is_usable_bash(found):
            return (found, ["-lc"], "posix")
    return (os.environ.get("COMSPEC", "cmd.exe"), ["/c"], "cmd")


def decode_output(raw: bytes) -> str:
    """Decode command output without mangling it.

    Not everything on Windows speaks UTF-8. Native tools emit the console
    codepage (GBK on a Chinese install) and some emit UTF-16, which decoded
    as UTF-8 turns a readable error message into a wall of replacement
    characters — the reason a WSL failure was unreadable rather than
    self-explanatory.

    Args:
      raw: The bytes the process wrote.

    Returns:
      Text, decoded with the first encoding that fits, falling back to a
      lossy UTF-8 read so output is never lost entirely.
    """
    if not raw:
        return ""

    # A byte-order mark settles it outright, so it is checked before any
    # guessing. This is the common case for the tools that emit UTF-16.
    for bom, encoding in BOMS:
        if raw.startswith(bom):
            with contextlib.suppress(UnicodeDecodeError):
                return raw.decode(encoding)

    # Without a BOM, NULs are the giveaway — but only for mostly-ASCII text.
    # CJK in UTF-16 has few of them, which is why a threshold cannot be the
    # primary test: a Chinese message sailed past it and was then "decoded"
    # by the codepage, which accepts almost any bytes and yields mojibake.
    sample = raw[:UTF16_SNIFF_BYTES]
    if 0 in sample:
        candidates = []
        for encoding in ("utf-16-le", "utf-16-be"):
            with contextlib.suppress(UnicodeDecodeError):
                candidates.append((_garbling(raw.decode(encoding)), raw.decode(encoding)))
        if candidates:
            score, text = min(candidates, key=lambda pair: pair[0])
            if score < GARBLING_LIMIT:
                return text

    strict = ("utf-8", locale.getpreferredencoding(False))
    for encoding in strict:
        if not encoding:
            continue
        with contextlib.suppress(UnicodeDecodeError, LookupError):
            return raw.decode(encoding)
    return raw.decode("utf-8", "replace")


def _garbling(text: str) -> float:
    """Share of characters that no real command would have printed.

    Used to tell a correct decode from one that merely did not raise. Control
    characters and replacement marks are the signature of the wrong codec.
    """
    if not text:
        return 1.0
    bad = sum(
        1
        for ch in text
        if ch == "�"
        or (ord(ch) < FIRST_PRINTABLE_CODEPOINT and ch not in "\r\n\t")
    )
    return bad / len(text)


def _truncate(text: str, limit: int = SHELL_MAX_OUTPUT_CHARS) -> str:
    """Keep the head and tail of over-long command output."""
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    dropped = len(text) - len(head) - len(tail)
    return f"{head}\n\n... [{dropped} characters truncated] ...\n\n{tail}"


class BashTool(Tool):
    name = "Bash"
    bulky = True
    description = """
Run a shell command in the workspace and return its combined output.

The working directory persists across calls within a session, but shell
state (variables, functions) does not — each call is a fresh shell.
Quote paths containing spaces. Prefer the Read/Glob/Grep tools over
cat/find/grep: they are faster and produce cleaner output.
Set `timeout` (seconds, max 600) for slow commands.
"""

    def __init__(self) -> None:
        self.executable, self.prefix, self.dialect = find_shell()
        self._cwd: Path | None = None

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The command line to run"},
                "description": {
                    "type": "string",
                    "description": "5-10 word description of what this does, shown to the user",
                },
                "timeout": {"type": "number", "description": "Seconds before the command is killed"},
                "cwd": {"type": "string", "description": "Working directory for this call"},
            },
            "required": ["command"],
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = str(args.get("command", "")).strip()
        if not command:
            return ToolResult.error("empty command")

        timeout = min(float(args.get("timeout") or SHELL_DEFAULT_TIMEOUT), SHELL_MAX_TIMEOUT)
        cwd = ctx.resolve(str(args["cwd"])) if args.get("cwd") else (self._cwd or ctx.workspace)
        if not cwd.is_dir():
            cwd = ctx.workspace

        label = str(args.get("description") or command[:SHELL_LABEL_CHARS])
        ctx.note(f"$ {command[:SHELL_COMMAND_ECHO_CHARS]}")

        env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env["AIH_AGENT"] = "1"  # so scripts can detect they are running under the agent

        # Snapshot for edit-review when the GUI board is attached.
        before_snap: dict = {}
        if getattr(ctx, "edit_review", None) is not None:
            from ..edits.side_effects import snapshot_workspace

            before_snap = await asyncio.to_thread(snapshot_workspace, ctx.workspace)

        # Track `cd` so the working directory persists like an interactive shell.
        probe = "; pwd" if self.dialect == "posix" else " & cd"
        wrapped = f"{command}{probe}"

        try:
            proc = await asyncio.create_subprocess_exec(
                self.executable,
                *self.prefix,
                wrapped,
                cwd=str(cwd),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                **hidden_subprocess_kwargs(),
            )
        except OSError as e:
            return ToolResult.error(f"cannot start shell {self.executable}: {e}")

        try:
            out, _ = await _communicate_or_cancel(proc, timeout, ctx.cancel)
        except asyncio.CancelledError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            raise
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            return ToolResult.error(
                f"command timed out after {timeout:.0f}s and was killed:\n$ {command}"
            )

        text = decode_output(out)
        lines = text.splitlines()

        # The trailing line is the pwd probe; peel it off and remember it.
        if lines:
            tail = lines[-1].strip()
            if tail and Path(tail).is_dir():
                self._cwd = Path(tail)
                lines = lines[:-1]
        body = "\n".join(lines).strip()

        side_effects = 0
        if before_snap:
            from ..edits.side_effects import queue_bash_side_effects

            side_effects = await asyncio.to_thread(
                queue_bash_side_effects, ctx, before_snap
            )

        code = proc.returncode or 0
        display = {
            "kind": "bash",
            "command": command,
            "exit": code,
            "side_effects": side_effects,
        }
        if code != 0:
            content = f"exit code {code}\n\n{_truncate(body) or '(no output)'}"
            return ToolResult(
                content=content,
                is_error=True,
                summary=f"{label} — failed (exit {code})",
                display=display,
            )

        return ToolResult(
            content=_truncate(body) or "(no output)",
            summary=f"{label} — ok",
            display=display,
        )


async def _communicate_or_cancel(
    proc: asyncio.subprocess.Process,
    timeout: float,
    cancel: asyncio.Event,
) -> tuple[bytes, bytes]:
    """Wait for ``proc`` output, aborting when the user interrupts."""
    if cancel.is_set():
        raise asyncio.CancelledError

    communicate = asyncio.create_task(proc.communicate())
    cancel_task = asyncio.create_task(cancel.wait())
    try:
        done, pending = await asyncio.wait(
            {communicate, cancel_task},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            communicate.cancel()
            cancel_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await communicate
            raise asyncio.TimeoutError
        if cancel_task in done or cancel.is_set():
            communicate.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await communicate
            raise asyncio.CancelledError
        cancel_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cancel_task
        return communicate.result()
    finally:
        if not communicate.done():
            communicate.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await communicate
        if not cancel_task.done():
            cancel_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cancel_task
