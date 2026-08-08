"""Deepening pass: review / index / quest / canvas / smoke hooks."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from aiharness.constants import QUEST_STEP_MAX_RETRIES
from aiharness.edits import EditReviewBoard
from aiharness.edits.diff import preview_for_kind, unified_hunk
from aiharness.market.bars import Bar, BarSeries
from aiharness.quest import (
    load_quest,
    start_quest,
    sync_quest_from_todos,
    sync_quest_from_verify,
)
from aiharness.workspace.paths import invalidate_path_index, list_paths


def test_unified_hunk_and_write_preview_truncation():
    hunk = unified_hunk("a\nb\n", "a\nc\n", path="x.py")
    assert "---" in hunk or "@@" in hunk or "a/x.py" in hunk
    big = "x" * 5000
    preview = preview_for_kind("write", big)
    assert len(preview) < len(big)
    assert preview.endswith("…")


def test_pending_edit_public_includes_unified(tmp_path: Path):
    path = tmp_path / "a.py"
    path.write_text("hello\n", encoding="utf-8")
    board = EditReviewBoard()
    item = board.add(
        path=path,
        rel="a.py",
        kind="edit",
        before="hello\n",
        after="world\n",
        old="hello",
        new="world",
        added=1,
        removed=1,
    )
    public = item.public()
    assert "unified" in public
    assert "world" in public["unified"] or "hello" in public["unified"]


def test_path_index_sorted_by_mtime(tmp_path: Path):
    invalidate_path_index()
    old = tmp_path / "old.txt"
    new = tmp_path / "new.txt"
    old.write_text("o", encoding="utf-8")
    new.write_text("n", encoding="utf-8")
    # Ensure mtime order: touch new after old.
    import os
    import time

    time.sleep(0.02)
    os.utime(new, None)
    paths = list_paths(tmp_path)
    files = [p for p in paths if p["kind"] == "file"]
    assert files[0]["path"] == "new.txt"
    assert files[0]["mtime"] >= files[1]["mtime"]


def test_quest_sync_todos_and_verify_fail(tmp_path: Path):
    start_quest(tmp_path, "Ship", ["investigate", "implement", "verify"])
    sync_quest_from_todos(
        tmp_path,
        [
            {"content": "investigate", "status": "completed"},
            {"content": "implement", "status": "in_progress"},
            {"content": "verify", "status": "pending"},
        ],
    )
    quest = load_quest(tmp_path)
    assert quest is not None
    assert quest.steps[0].status == "done"
    assert quest.steps[1].status == "active"

    retried = sync_quest_from_verify(tmp_path, verdict="FAIL — tests red", failures=2)
    assert retried is not None
    # First failure auto-retries instead of blocking. retry_pending is a
    # transient flag on the return value, never persisted to quest.json.
    assert retried.status == "active"
    assert retried.retry_pending is True
    assert retried.steps[1].attempts == 1

    for _ in range(QUEST_STEP_MAX_RETRIES):
        sync_quest_from_verify(tmp_path, verdict="FAIL — tests red", failures=2)
    blocked = load_quest(tmp_path)
    assert blocked is not None
    assert blocked.status == "blocked"
    assert blocked.steps[1].status == "failed"


def test_tray_module_importable():
    from aiharness.gui import tray

    assert hasattr(tray, "start")
    assert hasattr(tray, "Tray")


def test_bar_series_shape_for_kline():
    series = BarSeries(
        symbol="TEST",
        bars=[
            Bar(day=date(2024, 1, 2), open=1, high=2, low=0.5, close=1.5, volume=10),
            Bar(day=date(2024, 1, 3), open=1.5, high=2.5, low=1.2, close=2.0, volume=12),
        ],
    )
    payload = [
        {
            "day": bar.day.isoformat(),
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
        }
        for bar in series.bars
    ]
    assert len(payload) == 2
    assert payload[0]["day"] == "2024-01-02"
