"""Project memories: short facts the user pins for the agent."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

MEMORIES_REL = Path(".aiharness") / "memories.json"
MEMORIES_MAX = 40
MEMORY_MAX_CHARS = 500


@dataclass
class Memory:
    id: str
    text: str
    pinned: bool = True
    created_at: float = field(default_factory=time.time)

    def public(self) -> dict[str, Any]:
        return asdict(self)


def memories_path(workspace: Path) -> Path:
    return workspace / MEMORIES_REL


def load_memories(workspace: Path) -> list[Memory]:
    path = memories_path(workspace)
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items: list[Memory] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text", "")).strip()
        if not text:
            continue
        items.append(
            Memory(
                id=str(entry.get("id") or uuid.uuid4().hex[:8]),
                text=text[:MEMORY_MAX_CHARS],
                pinned=bool(entry.get("pinned", True)),
                created_at=float(entry.get("created_at") or time.time()),
            )
        )
    return items[:MEMORIES_MAX]


def save_memories(workspace: Path, memories: list[Memory]) -> Path:
    path = memories_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([m.public() for m in memories[:MEMORIES_MAX]], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def memories_section(workspace: Path) -> tuple[str, list[str]]:
    """Prompt section + source labels for pinned memories."""
    pinned = [m for m in load_memories(workspace) if m.pinned]
    if not pinned:
        return "", []
    lines = [f"- {m.text}" for m in pinned]
    section = (
        "# Memories\n\n"
        "Facts the user pinned. Prefer these over guesses when relevant.\n\n"
        + "\n".join(lines)
    )
    return section, [f"memory:{m.id}" for m in pinned]


def add_memory(workspace: Path, text: str, *, pinned: bool = True) -> Memory:
    memories = load_memories(workspace)
    item = Memory(id=uuid.uuid4().hex[:8], text=text.strip()[:MEMORY_MAX_CHARS], pinned=pinned)
    memories.insert(0, item)
    save_memories(workspace, memories)
    return item


def update_memory(workspace: Path, memory_id: str, *, text: str | None = None, pinned: bool | None = None) -> Memory | None:
    memories = load_memories(workspace)
    for item in memories:
        if item.id != memory_id:
            continue
        if text is not None:
            item.text = text.strip()[:MEMORY_MAX_CHARS]
        if pinned is not None:
            item.pinned = pinned
        save_memories(workspace, memories)
        return item
    return None


def delete_memory(workspace: Path, memory_id: str) -> bool:
    memories = load_memories(workspace)
    kept = [m for m in memories if m.id != memory_id]
    if len(kept) == len(memories):
        return False
    save_memories(workspace, kept)
    return True
