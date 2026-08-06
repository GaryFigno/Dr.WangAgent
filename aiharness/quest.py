"""Lightweight Quest: goal + steps with resume (local, no cloud)."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

QuestStatus = Literal["idle", "active", "blocked", "done"]
StepStatus = Literal["pending", "active", "done", "failed"]

QUEST_REL = Path(".aiharness") / "quest.json"


@dataclass
class QuestStep:
    id: str
    title: str
    status: StepStatus = "pending"
    note: str = ""

    def public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Quest:
    id: str
    goal: str
    status: QuestStatus = "idle"
    blocked_reason: str = ""
    steps: list[QuestStep] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "goal": self.goal,
            "status": self.status,
            "blocked_reason": self.blocked_reason,
            "steps": [s.public() for s in self.steps],
            "updated_at": self.updated_at,
            "active_step": self.active_step_title(),
        }

    def active_step_title(self) -> str:
        for step in self.steps:
            if step.status in {"active", "failed", "pending"}:
                return step.title
        return ""


def quest_path(workspace: Path) -> Path:
    return workspace / QUEST_REL


def load_quest(workspace: Path) -> Quest | None:
    path = quest_path(workspace)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or not raw.get("goal"):
        return None
    steps = [
        QuestStep(
            id=str(s.get("id") or uuid.uuid4().hex[:6]),
            title=str(s.get("title", "")),
            status=s.get("status") or "pending",  # type: ignore[arg-type]
            note=str(s.get("note") or ""),
        )
        for s in (raw.get("steps") or [])
        if isinstance(s, dict) and s.get("title")
    ]
    return Quest(
        id=str(raw.get("id") or uuid.uuid4().hex[:8]),
        goal=str(raw["goal"]),
        status=raw.get("status") or "idle",  # type: ignore[arg-type]
        blocked_reason=str(raw.get("blocked_reason") or ""),
        steps=steps,
        updated_at=float(raw.get("updated_at") or time.time()),
    )


def save_quest(workspace: Path, quest: Quest | None) -> None:
    path = quest_path(workspace)
    if quest is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    quest.updated_at = time.time()
    path.write_text(json.dumps(quest.public(), ensure_ascii=False, indent=2), encoding="utf-8")


def start_quest(workspace: Path, goal: str, steps: list[str]) -> Quest:
    quest = Quest(
        id=uuid.uuid4().hex[:8],
        goal=goal.strip(),
        status="active",
        steps=[
            QuestStep(
                id=uuid.uuid4().hex[:6],
                title=title.strip(),
                status="active" if index == 0 else "pending",
            )
            for index, title in enumerate(steps)
            if title.strip()
        ],
    )
    if not quest.steps:
        quest.steps = [QuestStep(id=uuid.uuid4().hex[:6], title="执行目标", status="active")]
    save_quest(workspace, quest)
    return quest


def set_step_status(
    workspace: Path,
    step_id: str,
    status: StepStatus,
    *,
    note: str = "",
    blocked_reason: str = "",
) -> Quest | None:
    quest = load_quest(workspace)
    if quest is None:
        return None
    for step in quest.steps:
        if step.id != step_id:
            continue
        step.status = status
        if note:
            step.note = note
        break
    else:
        return None
    if status == "failed":
        quest.status = "blocked"
        quest.blocked_reason = blocked_reason or note or "步骤失败"
    elif status == "done":
        quest.blocked_reason = ""
        # Advance: mark next pending as active
        for step in quest.steps:
            if step.status == "pending":
                step.status = "active"
                quest.status = "active"
                break
        else:
            if all(s.status == "done" for s in quest.steps):
                quest.status = "done"
            else:
                quest.status = "active"
    else:
        quest.status = "active"
        quest.blocked_reason = ""
    save_quest(workspace, quest)
    return quest


def resume_quest(workspace: Path) -> Quest | None:
    """Clear blocked state and focus the first failed/pending step."""
    quest = load_quest(workspace)
    if quest is None:
        return None
    for step in quest.steps:
        if step.status == "failed":
            step.status = "active"
            step.note = ""
            break
        if step.status == "pending":
            step.status = "active"
            break
        if step.status == "active":
            break
    quest.status = "active"
    quest.blocked_reason = ""
    save_quest(workspace, quest)
    return quest


def quest_prompt_hint(workspace: Path) -> str:
    quest = load_quest(workspace)
    if quest is None or quest.status in {"idle", "done"}:
        return ""
    active = quest.active_step_title() or "(无步骤)"
    blocked = f"\nBlocked: {quest.blocked_reason}" if quest.blocked_reason else ""
    return (
        f"[Active Quest]\nGoal: {quest.goal}\nCurrent step: {active}"
        f"{blocked}\nContinue from this step; do not restart completed work.\n\n"
    )


def sync_quest_from_todos(workspace: Path, todos: list[dict[str, Any]]) -> Quest | None:
    """Mirror TodoWrite into an active Quest (create steps if needed)."""
    quest = load_quest(workspace)
    if quest is None or quest.status in {"idle", "done"}:
        return None
    if not todos:
        return quest
    # Align step statuses by order when counts match; otherwise keep quest steps.
    if len(todos) == len(quest.steps):
        for step, todo in zip(quest.steps, todos, strict=True):
            status = str(todo.get("status", "pending"))
            if status == "completed":
                step.status = "done"
            elif status == "in_progress":
                step.status = "active"
            else:
                step.status = "pending"
        if any(s.status == "active" for s in quest.steps):
            quest.status = "active"
            quest.blocked_reason = ""
        elif all(s.status == "done" for s in quest.steps):
            quest.status = "done"
        save_quest(workspace, quest)
        return quest
    # Partial sync: mark active todo title as active step when titles overlap.
    active_titles = {
        str(t.get("content", "")).strip().lower()
        for t in todos
        if t.get("status") == "in_progress"
    }
    done_titles = {
        str(t.get("content", "")).strip().lower()
        for t in todos
        if t.get("status") == "completed"
    }
    for step in quest.steps:
        key = step.title.strip().lower()
        if key in done_titles:
            step.status = "done"
        elif key in active_titles:
            step.status = "active"
            quest.status = "active"
            quest.blocked_reason = ""
    save_quest(workspace, quest)
    return quest


def sync_quest_from_verify(
    workspace: Path, *, verdict: str, failures: int = 0
) -> Quest | None:
    """Mark the active Quest step failed/done from a Verify result."""
    quest = load_quest(workspace)
    if quest is None or quest.status in {"idle", "done"}:
        return None
    active = next((s for s in quest.steps if s.status == "active"), None)
    if active is None:
        active = next((s for s in quest.steps if s.status == "pending"), None)
    if active is None:
        return quest
    upper = (verdict or "").upper()
    failed = upper.startswith("FAIL") or (
        failures > 0 and "PASS" not in upper and "UNKNOWN" not in upper
    )
    if failed:
        return set_step_status(
            workspace,
            active.id,
            "failed",
            note=verdict,
            blocked_reason=f"Verify: {verdict}",
        )
    if "PASS" in upper:
        return set_step_status(workspace, active.id, "done", note=verdict)
    return quest
