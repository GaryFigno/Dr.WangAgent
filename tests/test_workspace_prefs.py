"""Workspace preference helpers — never treat the app install dir as a project."""

from __future__ import annotations

from pathlib import Path

from aiharness.gui.workspace import (
    RecentWorkspaces,
    is_app_install_workspace,
    preferred_project_workspace,
)


def test_is_app_install_workspace_detects_bundle_name(tmp_path: Path):
    bundle = tmp_path / "Dr.Wang"
    bundle.mkdir()
    (bundle / "Dr.Wang.exe").write_bytes(b"x")
    assert is_app_install_workspace(bundle)
    project = tmp_path / "FPSRoguelike"
    project.mkdir()
    assert not is_app_install_workspace(project)


def test_remember_skips_install_dir(tmp_path: Path, monkeypatch):
    recents_file = tmp_path / "workspaces.json"
    monkeypatch.setenv("AIH_WORKSPACES_FILE", str(recents_file))
    bundle = tmp_path / "Dr.Wang"
    bundle.mkdir()
    (bundle / "Dr.Wang.exe").write_bytes(b"x")
    project = tmp_path / "DiabloGame"
    project.mkdir()
    stored = RecentWorkspaces()
    stored.remember(bundle)
    stored.remember(project)
    stored.save()
    loaded = RecentWorkspaces.load()
    assert str(project) in loaded.paths
    assert str(bundle) not in loaded.paths


def test_preferred_skips_install_fallback(tmp_path: Path, monkeypatch):
    recents_file = tmp_path / "workspaces.json"
    monkeypatch.setenv("AIH_WORKSPACES_FILE", str(recents_file))
    monkeypatch.setenv("AIH_CODEX_SESSION_DIR", str(tmp_path / "codex_sessions"))
    monkeypatch.setenv("AIH_CLAUDE_SESSION_DIR", str(tmp_path / "claude_sessions"))
    bundle = tmp_path / "Dr.Wang"
    bundle.mkdir()
    (bundle / "aih.exe").write_bytes(b"x")
    project = tmp_path / "FPSRoguelike"
    project.mkdir()
    RecentWorkspaces(paths=[str(project)]).save()
    chosen = preferred_project_workspace(bundle)
    assert chosen.resolve() == project.resolve()
