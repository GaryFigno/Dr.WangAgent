"""gitignore ignore matcher, nested instructions, microcompact, repo map."""

from __future__ import annotations

from pathlib import Path

from aiharness.agent.context import microcompact_reads
from aiharness.agent.prompts import build_environment_note, read_project_instructions
from aiharness.providers.base import Message
from aiharness.workspace.ignore import IgnoreMatcher
from aiharness.workspace.repomap import build_repo_map


def test_gitignore_patterns_are_honoured(tmp_path: Path):
    (tmp_path / ".gitignore").write_text("secret/\n*.log\n", encoding="utf-8")
    (tmp_path / "secret").mkdir()
    (tmp_path / "secret" / "key.txt").write_text("x", encoding="utf-8")
    (tmp_path / "keep.py").write_text("print(1)\n", encoding="utf-8")
    (tmp_path / "noise.log").write_text("log\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("1", encoding="utf-8")

    matcher = IgnoreMatcher.for_workspace(tmp_path)
    assert matcher.is_ignored(tmp_path / "secret" / "key.txt")
    assert matcher.is_ignored(tmp_path / "noise.log")
    assert matcher.is_ignored(tmp_path / "node_modules" / "x.js")
    assert not matcher.is_ignored(tmp_path / "keep.py")


def test_nested_agents_md_is_loaded(tmp_path: Path):
    (tmp_path / "AGENTS.md").write_text("root rules", encoding="utf-8")
    nested = tmp_path / "pkg"
    nested.mkdir()
    (nested / "AGENTS.md").write_text("pkg rules", encoding="utf-8")
    text = read_project_instructions(tmp_path)
    assert "root rules" in text
    assert "pkg rules" in text
    assert "pkg/AGENTS.md" in text or "pkg\\AGENTS.md" in text


def test_microcompact_collapses_older_reads_of_same_path():
    messages = [
        Message(
            role="tool",
            content="1\told body " + ("x" * 200),
            name="Read",
            tool_call_id="1",
            meta={"tool": "Read", "path": "a.py"},
        ),
        Message(
            role="tool",
            content="1\tmid body",
            name="Read",
            tool_call_id="2",
            meta={"tool": "Read", "path": "a.py"},
        ),
        Message(
            role="tool",
            content="1\tnewest",
            name="Read",
            tool_call_id="3",
            meta={"tool": "Read", "path": "a.py"},
        ),
        Message(
            role="tool",
            content="1\tother",
            name="Read",
            tool_call_id="4",
            meta={"tool": "Read", "path": "b.py"},
        ),
    ]
    n = microcompact_reads(messages, keep_recent=2)
    assert n == 1
    assert messages[0].meta.get("microcompacted") is True
    assert messages[0].content.startswith("[microcompact]")
    assert messages[1].content == "1\tmid body"
    assert messages[2].content == "1\tnewest"
    assert messages[3].content == "1\tother"


def test_environment_note_includes_repo_map(tmp_path: Path):
    (tmp_path / "auth_service.py").write_text(
        "class AuthService:\n    def login(self):\n        pass\n",
        encoding="utf-8",
    )
    note = build_environment_note(tmp_path, query="fix auth login")
    assert "Repo map" in note
    assert "auth_service.py" in note


def test_repo_map_ranks_query_hits(tmp_path: Path):
    (tmp_path / "auth.py").write_text("def login():\n    return 1\n", encoding="utf-8")
    (tmp_path / "unrelated.py").write_text("def foo():\n    return 2\n", encoding="utf-8")
    text = build_repo_map(tmp_path, "login auth")
    assert "auth.py" in text
    if "unrelated.py" in text:
        assert text.index("auth.py") < text.index("unrelated.py")
