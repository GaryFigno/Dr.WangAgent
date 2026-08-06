"""User/project rules loading."""

from __future__ import annotations

from aiharness.rules import load_rules


def test_project_rules_are_loaded(tmp_path, monkeypatch):
    monkeypatch.setenv("AIH_RULES_DIR", str(tmp_path / "global-rules"))
    (tmp_path / "global-rules").mkdir()
    (tmp_path / "global-rules" / "tone.md").write_text("Be brief.", encoding="utf-8")
    project_rules = tmp_path / ".aiharness" / "rules"
    project_rules.mkdir(parents=True)
    (project_rules / "style.md").write_text("Prefer Google style.", encoding="utf-8")

    section, sources = load_rules(tmp_path)
    assert "Be brief." in section
    assert "Prefer Google style." in section
    assert "global:tone.md" in sources
    assert "project:style.md" in sources


def test_missing_rules_dirs_yield_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("AIH_RULES_DIR", str(tmp_path / "missing"))
    section, sources = load_rules(tmp_path)
    assert section == ""
    assert sources == []
