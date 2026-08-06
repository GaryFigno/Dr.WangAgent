"""Gap-fill: bash side-effects, content search, backtest, alerts, messages."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from aiharness.edits import EditReviewBoard
from aiharness.edits.side_effects import (
    collect_side_effects,
    queue_bash_side_effects,
    snapshot_workspace,
)
from aiharness.gui.messages import tr
from aiharness.market.alerts import add_alert, check_alerts, load_alerts
from aiharness.market.backtest import ma_cross_backtest
from aiharness.market.bars import Bar, BarSeries
from aiharness.workspace.paths import list_paths
from aiharness.workspace.search import search_content


def test_tr_falls_back_and_formats():
    assert "interrupt" in tr("en", "busy").lower() or "Interrupt" in tr("en", "busy")
    assert "Quest" in tr("ja", "quest.resumed") or "再開" in tr("ja", "quest.resumed")
    assert "demo" in tr("en", "quest.started", goal="demo")


def test_content_search_and_ext_filter(tmp_path: Path):
    (tmp_path / "a.py").write_text("def hello_world():\n    pass\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("hello_world docs\n", encoding="utf-8")
    hits = search_content(tmp_path, "hello_world", glob=".py")
    assert len(hits) == 1
    assert hits[0]["path"] == "a.py"
    paths = list_paths(tmp_path, query="a", ext="py", kind="file")
    assert any(p["path"] == "a.py" for p in paths)


def test_bash_side_effect_queue(tmp_path: Path):
    path = tmp_path / "out.txt"
    path.write_text("before\n", encoding="utf-8")
    before = snapshot_workspace(tmp_path)
    path.write_text("after\n", encoding="utf-8")
    changes = collect_side_effects(tmp_path, before)
    assert any(c["rel"] == "out.txt" for c in changes)
    board = EditReviewBoard()

    class _Ctx:
        workspace = tmp_path
        edit_review = board
        current_call_id = "bash1"

    n = queue_bash_side_effects(_Ctx(), before)
    assert n >= 1
    assert board.pending()


def test_ma_cross_backtest_equity():
    bars = []
    price = 100.0
    for i in range(60):
        price += 1.0 if i % 7 < 4 else -0.5
        bars.append(
            Bar(
                day=date(2024, 1, 1).fromordinal(date(2024, 1, 1).toordinal() + i),
                open=price,
                high=price + 1,
                low=price - 1,
                close=price,
                volume=1000,
            )
        )
    series = BarSeries(symbol="TEST", bars=bars)
    result = ma_cross_backtest(series, fast=3, slow=8)
    assert len(result.equity) == 60
    assert "TEST" in result.describe()


def test_alerts_fire_once(tmp_path: Path):
    alert = add_alert(tmp_path, "AAPL", 100.0, op=">=")
    assert load_alerts(tmp_path)
    fired = check_alerts(tmp_path, "AAPL", 101.0)
    assert len(fired) == 1
    assert fired[0][0].id == alert.id
    assert check_alerts(tmp_path, "AAPL", 102.0) == []
