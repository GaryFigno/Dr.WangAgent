"""Memories and Quest MVP."""

from pathlib import Path

from aiharness.constants import QUEST_STEP_MAX_RETRIES
from aiharness.memories import (
    add_memory,
    delete_memory,
    load_memories,
    memories_section,
)
from aiharness.quest import (
    load_quest,
    quest_prompt_hint,
    resume_quest,
    set_step_status,
    start_quest,
    sync_quest_from_verify,
)


def test_memory_round_trip(tmp_path: Path):
    item = add_memory(tmp_path, "prefer pytest")
    assert item.text == "prefer pytest"
    section, sources = memories_section(tmp_path)
    assert "prefer pytest" in section
    assert sources
    assert delete_memory(tmp_path, item.id)
    assert load_memories(tmp_path) == []


def test_quest_resume_after_failure(tmp_path: Path):
    quest = start_quest(tmp_path, "Ship feature", ["investigate", "implement", "verify"])
    first = quest.steps[0].id
    set_step_status(tmp_path, first, "failed", note="boom", blocked_reason="tests red")
    blocked = load_quest(tmp_path)
    assert blocked is not None
    assert blocked.status == "blocked"
    resumed = resume_quest(tmp_path)
    assert resumed is not None
    assert resumed.status == "active"
    assert resumed.steps[0].status == "active"
    hint = quest_prompt_hint(tmp_path)
    assert "Ship feature" in hint
    assert "investigate" in hint


def test_verify_failure_retries_then_blocks(tmp_path: Path):
    start_quest(tmp_path, "Ship feature", ["implement", "verify"])
    quest = sync_quest_from_verify(tmp_path, verdict="FAIL: tests red", failures=1)
    assert quest is not None
    assert quest.status == "active"
    assert quest.retry_pending is True
    assert quest.steps[0].attempts == 1
    assert quest.blocked_reason == ""

    quest = sync_quest_from_verify(tmp_path, verdict="FAIL: still red", failures=1)
    assert quest is not None
    assert quest.status == "active"
    assert quest.steps[0].attempts == 2

    quest = sync_quest_from_verify(tmp_path, verdict="FAIL: still red", failures=1)
    assert quest is not None
    assert quest.status == "blocked"
    assert quest.steps[0].status == "failed"
    assert quest.steps[0].attempts == QUEST_STEP_MAX_RETRIES + 1
    assert "still red" in quest.blocked_reason


def test_verify_pass_advances_without_retry(tmp_path: Path):
    start_quest(tmp_path, "Ship feature", ["implement", "verify"])
    quest = sync_quest_from_verify(tmp_path, verdict="PASS", failures=0)
    assert quest is not None
    assert quest.status == "active"  # second step now active
    assert quest.retry_pending is False
    assert quest.steps[0].status == "done"
    assert quest.steps[0].attempts == 0


def test_quest_attempts_round_trip(tmp_path: Path):
    start_quest(tmp_path, "Ship", ["a"])
    sync_quest_from_verify(tmp_path, verdict="FAIL", failures=1)
    loaded = load_quest(tmp_path)
    assert loaded is not None
    assert loaded.steps[0].attempts == 1
    # retry_pending is transient: never persisted.
    assert "retry_pending" not in (tmp_path / ".aiharness" / "quest.json").read_text(
        encoding="utf-8"
    )


def test_resume_resets_attempts(tmp_path: Path):
    start_quest(tmp_path, "Ship", ["a"])
    for _ in range(QUEST_STEP_MAX_RETRIES + 1):
        sync_quest_from_verify(tmp_path, verdict="FAIL", failures=1)
    blocked = load_quest(tmp_path)
    assert blocked is not None and blocked.status == "blocked"
    resumed = resume_quest(tmp_path)
    assert resumed is not None
    assert resumed.status == "active"
    assert resumed.steps[0].attempts == 0
