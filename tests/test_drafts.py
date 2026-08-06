"""Composer draft persistence (unit level)."""

from __future__ import annotations

from aiharness.gui.drafts import DraftStore


def test_draft_store_round_trips(tmp_path):
    store = DraftStore(tmp_path / "drafts.json")
    store.set("s1", "hello draft")
    store.set("s2", "other")
    assert store.get("s1") == "hello draft"
    assert DraftStore(tmp_path / "drafts.json").get("s1") == "hello draft"
    store.clear("s1")
    assert store.get("s1") == ""
    assert store.get("s2") == "other"


def test_empty_draft_is_removed_from_disk(tmp_path):
    path = tmp_path / "drafts.json"
    store = DraftStore(path)
    store.set("s1", "temporary")
    store.set("s1", "   ")
    assert "s1" not in path.read_text(encoding="utf-8")


def test_draft_is_capped(tmp_path):
    from aiharness.constants import COMPOSER_DRAFT_MAX_CHARS

    store = DraftStore(tmp_path / "drafts.json")
    store.set("s1", "x" * (COMPOSER_DRAFT_MAX_CHARS + 50))
    assert len(store.get("s1")) == COMPOSER_DRAFT_MAX_CHARS
