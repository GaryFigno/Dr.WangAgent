"""Tests for orphan Claude/Codex CLI cleanup."""

from __future__ import annotations

from pathlib import Path

from aiharness.gui.cli_orphans import (
    KIND_CLAUDE,
    KIND_CODEX,
    classify_command,
    find_orphans,
    register_child,
    reap_orphans,
    summarize_reaped,
    unregister_child,
)


def test_classify_headless_fingerprints():
    claude = (
        "node claude --print --verbose --input-format stream-json "
        "--output-format stream-json --permission-prompt-tool stdio"
    )
    assert classify_command(claude) == KIND_CLAUDE
    assert classify_command("codex app-server --listen stdio://") == KIND_CODEX
    # Interactive / unrelated must not match.
    assert classify_command("claude") is None
    assert classify_command("codex") is None
    assert classify_command("node some-other-tool --print") is None


def test_registry_roundtrip(tmp_path: Path):
    register_child(424242, KIND_CLAUDE, command="claude --print", root=tmp_path)
    register_child(424243, KIND_CODEX, command="codex app-server", root=tmp_path)
    unregister_child(424242, root=tmp_path)
    # Dead pids are ignored by find_orphans even if still registered.
    hits = find_orphans(root=tmp_path, processes=[])
    assert hits == []


def test_find_orphans_from_fingerprint_scan(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "aiharness.gui.cli_orphans._pid_alive",
        lambda pid: pid == 99,
    )
    processes = [
        (
            99,
            "node.exe",
            "node claude --print --input-format stream-json --output-format stream-json",
        ),
        (100, "codex.exe", "codex app-server --listen stdio://"),  # treated dead
        (101, "node.exe", "node something-else"),
    ]
    hits = find_orphans(root=tmp_path, processes=processes, kinds=(KIND_CLAUDE, KIND_CODEX))
    assert len(hits) == 1
    assert hits[0].pid == 99
    assert hits[0].kind == KIND_CLAUDE
    assert hits[0].reason == "fingerprint"


def test_reap_orphans_kills_and_clears_registry(tmp_path: Path, monkeypatch):
    register_child(77, KIND_CODEX, command="codex app-server --listen stdio://", root=tmp_path)
    alive = {77}
    killed_pids: list[int] = []

    monkeypatch.setattr(
        "aiharness.gui.cli_orphans._pid_alive",
        lambda pid: pid in alive,
    )

    def kill_and_mark(pid: int) -> bool:
        killed_pids.append(pid)
        alive.discard(pid)
        return True

    monkeypatch.setattr("aiharness.gui.cli_orphans._kill_pid", kill_and_mark)
    hits = reap_orphans(root=tmp_path, processes=[], kinds=(KIND_CODEX,))
    assert [hit.pid for hit in hits] == [77]
    assert killed_pids == [77]
    assert summarize_reaped(hits)
    assert find_orphans(root=tmp_path, processes=[]) == []


def test_keep_pids_are_spared(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("aiharness.gui.cli_orphans._pid_alive", lambda _pid: True)
    processes = [
        (55, "node.exe", "node x --print --input-format stream-json --output-format stream-json"),
    ]
    hits = find_orphans(root=tmp_path, processes=processes, keep_pids={55})
    assert hits == []


def test_registry_pid_reuse_by_desktop_is_not_killed(tmp_path: Path, monkeypatch):
    """If OS reuses our old PID for Claude Desktop, do not kill it."""
    register_child(
        88,
        KIND_CLAUDE,
        command="node claude --print --input-format stream-json --output-format stream-json",
        root=tmp_path,
    )
    monkeypatch.setattr("aiharness.gui.cli_orphans._pid_alive", lambda pid: pid == 88)
    # Live process at pid 88 is a desktop-like command without headless markers.
    processes = [(88, "Claude.exe", "C:\\Users\\x\\AppData\\Local\\AnthropicClaude\\Claude.exe")]
    hits = find_orphans(root=tmp_path, processes=processes)
    assert hits == []
