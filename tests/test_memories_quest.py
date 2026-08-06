"""Memories and Quest MVP."""

from pathlib import Path

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
