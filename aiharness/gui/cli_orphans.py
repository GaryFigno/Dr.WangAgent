"""Reap orphan Claude Code / Codex CLI processes after a GUI crash.

Dr.Wang owns those CLIs over stdin/stdout. A hard crash leaves the children
alive with broken pipes; the next launch cannot re-attach, so we kill known
orphans and resume the saved session id in a fresh process.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from platformdirs import user_data_dir

from ..process import hidden_subprocess_kwargs

log = logging.getLogger(__name__)

REGISTRY_NAME = "cli_children.json"
KIND_CLAUDE = "claude"
KIND_CODEX = "codex"

# Headless signatures we spawn — interactive user terminals must not match.
_CLAUDE_MARKERS = ("stream-json", "--input-format", "--print")
_CODEX_MARKERS = ("app-server", "stdio://")


@dataclass(frozen=True)
class OrphanHit:
    pid: int
    kind: str
    reason: str
    command: str = ""


def registry_path(root: Path | None = None) -> Path:
    base = root or Path(user_data_dir("aiharness", appauthor=False))
    return base / REGISTRY_NAME


def _read_registry(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


def _write_registry(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def register_child(
    pid: int,
    kind: str,
    *,
    command: str = "",
    root: Path | None = None,
) -> None:
    """Record a CLI child we own so a later crash can find it."""
    if pid <= 0:
        return
    path = registry_path(root)
    rows = [row for row in _read_registry(path) if int(row.get("pid") or 0) != pid]
    rows.append(
        {
            "pid": int(pid),
            "kind": kind,
            "command": (command or "")[:500],
            "registered_at": time.time(),
        }
    )
    try:
        _write_registry(path, rows)
    except OSError as error:
        log.warning("cli orphan register failed: %s", error)


def unregister_child(pid: int | None, *, root: Path | None = None) -> None:
    if not pid:
        return
    path = registry_path(root)
    rows = _read_registry(path)
    kept = [row for row in rows if int(row.get("pid") or 0) != int(pid)]
    if len(kept) == len(rows):
        return
    try:
        _write_registry(path, kept)
    except OSError as error:
        log.warning("cli orphan unregister failed: %s", error)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # On Windows, os.kill(pid, 0) is not a reliable existence check.
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            kernel32.CloseHandle(handle)
            return True
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _kill_pid(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=15,
                **hidden_subprocess_kwargs(),
            )
            return result.returncode == 0 or not _pid_alive(pid)
        os.kill(pid, signal.SIGTERM)
        deadline = time.time() + 3
        while time.time() < deadline and _pid_alive(pid):
            time.sleep(0.05)
        if _pid_alive(pid):
            os.kill(pid, signal.SIGKILL)
        return not _pid_alive(pid)
    except Exception as error:  # noqa: BLE001
        log.warning("failed to kill orphan pid=%s: %s", pid, error)
        return False


def _list_candidate_processes() -> list[tuple[int, str, str]]:
    """Return ``(pid, name, commandline)`` for likely Claude/Codex hosts."""
    if sys.platform == "win32":
        return _list_processes_win()
    return _list_processes_posix()


def _list_processes_win() -> list[tuple[int, str, str]]:
    script = (
        "$ErrorActionPreference='SilentlyContinue'; "
        "[Console]::OutputEncoding=[Text.UTF8Encoding]::UTF8; "
        "Get-CimInstance Win32_Process | "
        "Where-Object { "
        "  $_.Name -match '^(node|claude|codex|aih)\\.' "
        "  -or ($_.CommandLine -match 'stream-json|app-server') "
        "} | "
        "Select-Object ProcessId, Name, CommandLine | "
        "ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            timeout=20,
            encoding="utf-8",
            errors="replace",
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        log.warning("orphan process scan failed: %s", error)
        return []
    text = (result.stdout or "").strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        return []
    out: list[tuple[int, str, str]] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        try:
            pid = int(row.get("ProcessId") or 0)
        except (TypeError, ValueError):
            continue
        name = str(row.get("Name") or "")
        cmd = str(row.get("CommandLine") or "")
        if pid > 0:
            out.append((pid, name, cmd))
    return out


def _list_processes_posix() -> list[tuple[int, str, str]]:
    out: list[tuple[int, str, str]] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return out
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode(
                "utf-8", "replace"
            )
            comm = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if any(marker in cmdline for marker in ("stream-json", "app-server", "claude", "codex")):
            out.append((pid, comm, cmdline))
    return out


def classify_command(command: str) -> str | None:
    """Return kind if *command* matches a Dr.Wang headless CLI fingerprint."""
    text = (command or "").lower()
    if not text:
        return None
    if all(marker in text for marker in _CLAUDE_MARKERS):
        return KIND_CLAUDE
    if all(marker in text for marker in _CODEX_MARKERS):
        return KIND_CODEX
    return None


def find_orphans(
    *,
    keep_pids: Iterable[int] | None = None,
    kinds: Iterable[str] | None = None,
    root: Path | None = None,
    processes: list[tuple[int, str, str]] | None = None,
) -> list[OrphanHit]:
    """Discover orphans from the pid registry and a live process scan."""
    keep = {int(pid) for pid in (keep_pids or []) if int(pid) > 0}
    keep.add(os.getpid())
    allow = {str(k) for k in (kinds or (KIND_CLAUDE, KIND_CODEX))}
    path = registry_path(root)
    hits: dict[int, OrphanHit] = {}

    live = processes if processes is not None else _list_candidate_processes()
    live_by_pid = {pid: (name, command) for pid, name, command in live}

    # Registry hits must still look like our headless CLIs. A PID can be reused
    # by Claude/Codex Desktop after a crash — never kill on pid alone.
    for row in _read_registry(path):
        try:
            pid = int(row.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        kind = str(row.get("kind") or "")
        if pid in keep or kind not in allow:
            continue
        if not _pid_alive(pid):
            continue
        registered_cmd = str(row.get("command") or "")
        live_cmd = live_by_pid.get(pid, ("", ""))[1]
        cmd = live_cmd or registered_cmd
        matched = classify_command(cmd)
        if matched != kind:
            # Stale registry row (process exited and pid reused, or desktop app).
            continue
        hits[pid] = OrphanHit(
            pid=pid,
            kind=kind,
            reason="registry",
            command=cmd[:500],
        )

    for pid, _name, command in live:
        if pid in keep or pid in hits:
            continue
        if not _pid_alive(pid):
            continue
        kind = classify_command(command)
        if kind is None or kind not in allow:
            continue
        hits[pid] = OrphanHit(pid=pid, kind=kind, reason="fingerprint", command=command[:500])

    return sorted(hits.values(), key=lambda item: item.pid)


def reap_orphans(
    *,
    keep_pids: Iterable[int] | None = None,
    kinds: Iterable[str] | None = None,
    root: Path | None = None,
    processes: list[tuple[int, str, str]] | None = None,
) -> list[OrphanHit]:
    """Kill discovered orphans and drop them from the registry."""
    found = find_orphans(
        keep_pids=keep_pids,
        kinds=kinds,
        root=root,
        processes=processes,
    )
    killed: list[OrphanHit] = []
    for hit in found:
        if _kill_pid(hit.pid):
            killed.append(hit)
            unregister_child(hit.pid, root=root)
            log.info("reaped orphan %s pid=%s (%s)", hit.kind, hit.pid, hit.reason)
        else:
            log.warning("could not reap orphan %s pid=%s", hit.kind, hit.pid)

    # Drop stale registry rows for dead pids.
    path = registry_path(root)
    rows = _read_registry(path)
    alive_rows = [row for row in rows if _pid_alive(int(row.get("pid") or 0))]
    if len(alive_rows) != len(rows):
        try:
            _write_registry(path, alive_rows)
        except OSError:
            pass
    return killed


def summarize_reaped(hits: list[OrphanHit]) -> str:
    if not hits:
        return ""
    parts = [f"{hit.kind}:{hit.pid}" for hit in hits]
    return f"已清理 {len(hits)} 个残留 CLI（{'、'.join(parts)}），将按会话续聊"
