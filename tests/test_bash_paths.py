"""Bash path extraction and outside-workspace prompting."""

from __future__ import annotations

from aiharness.config.schema import PermissionConfig
from aiharness.permissions import Decision, PermissionEngine, extract_command_paths


def test_extract_command_paths_finds_relative_and_absolute():
    paths = extract_command_paths('cat ./src/main.py && cp /tmp/x.txt out.txt')
    assert "./src/main.py" in paths
    assert "/tmp/x.txt" in paths
    assert "out.txt" in paths


def test_bash_outside_workspace_asks_in_auto(workspace, tmp_path):
    outside = tmp_path.parent / "elsewhere" / "secret.txt"
    outside.parent.mkdir(exist_ok=True)
    outside.write_text("x", encoding="utf-8")
    engine = PermissionEngine(PermissionConfig(mode="auto"), workspace)
    verdict = engine.check("Bash", {"command": f"cat {outside}"})
    assert verdict.decision is Decision.ASK
    assert "outside the workspace" in verdict.reason


def test_bash_workspace_relative_stays_allowed(workspace):
    engine = PermissionEngine(PermissionConfig(mode="auto"), workspace)
    (workspace / "notes.txt").write_text("hi", encoding="utf-8")
    verdict = engine.check("Bash", {"command": "cat notes.txt"})
    assert verdict.decision is Decision.ALLOW


def test_explore_mode_blocks_writes(workspace):
    engine = PermissionEngine(PermissionConfig(mode="yolo"), workspace)
    engine.set_explore_mode(True)
    assert engine.check("Read", {"file_path": "a.txt"}).decision is Decision.ALLOW
    assert engine.check("Write", {"file_path": "a.txt", "content": "x"}).decision is Decision.DENY
    assert engine.check("Bash", {"command": "rm notes.txt"}).decision is Decision.DENY
    assert engine.check("PresentPlan", {}).decision is Decision.DENY
