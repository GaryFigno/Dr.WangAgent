"""Tests for isolated Codex / Claude panel session store."""

from __future__ import annotations

from pathlib import Path

from aiharness.gui.panel_sessions import PanelSessionStore, PanelTranscriptEntry


def test_create_list_touch_archive_delete(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AIH_CODEX_SESSION_DIR", str(tmp_path / "codex"))
    store = PanelSessionStore("codex", root=tmp_path / "codex")
    ws = tmp_path / "proj"
    ws.mkdir()
    meta = store.create(ws, title="hello")
    assert meta.id
    assert store.get(meta.id) is not None
    listed = store.list(workspace=ws)
    assert len(listed) == 1
    store.touch(meta.id, native_id="thread-abc", title="updated")
    again = store.get(meta.id)
    assert again is not None
    assert again.native_id == "thread-abc"
    assert again.title == "updated"
    store.set_archived(meta.id, True)
    assert store.list(workspace=ws, include_archived=False) == []
    assert store.list(workspace=ws, include_archived=True)
    assert store.delete(meta.id)
    assert store.get(meta.id) is None


def test_transcript_and_ui_groups(tmp_path: Path):
    store = PanelSessionStore("claude", root=tmp_path / "claude")
    ws = tmp_path / "w"
    ws.mkdir()
    a = store.create(ws, title="a")
    b = store.create(ws, title="b")
    store.append_transcript(a.id, PanelTranscriptEntry(role="user", text="hi"))
    store.append_transcript(a.id, PanelTranscriptEntry(role="assistant", text="yo"))
    rows = store.load_transcript(a.id)
    assert len(rows) == 2
    assert rows[0]["text"] == "hi"
    groups = store.ui_groups(
        viewed_id=a.id,
        viewed_workspace=str(ws),
        running_ids=[b.id],
    )
    assert groups["viewed_id"] == a.id
    assert groups["workspaces"]
    sessions = groups["workspaces"][0]["sessions"]
    by_id = {row["id"]: row for row in sessions}
    assert by_id[a.id]["active"] is True
    assert by_id[b.id]["running"] is True
