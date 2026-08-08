"""Lightweight Quest: goal + steps with resume (local, no cloud).

Quests are scoped per chat when ``session_id`` is provided
(``.aiharness/quests/<session_id>.json``). The legacy workspace-wide
``.aiharness/quest.json`` remains the default for callers that omit it
(tests and older tooling).
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from .constants import QUEST_STEP_MAX_RETRIES

QuestStatus = Literal["idle", "active", "blocked", "done"]
StepStatus = Literal["pending", "active", "done", "failed"]

QUEST_REL = Path(".aiharness") / "quest.json"
QUESTS_DIR = Path(".aiharness") / "quests"


@dataclass
class QuestStep:
    id: str
    title: str
    status: StepStatus = "pending"
    note: str = ""
    attempts: int = 0

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
    #: Owning chat id when scoped; empty for legacy workspace-wide quests.
    session_id: str = ""
    #: Set by sync_quest_from_verify when a failure was auto-retried; the GUI
    #: consumes it to start the next turn. Never persisted (public() omits it).
    retry_pending: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "goal": self.goal,
            "status": self.status,
            "blocked_reason": self.blocked_reason,
            "steps": [s.public() for s in self.steps],
            "updated_at": self.updated_at,
            "active_step": self.active_step_title(),
            "session_id": self.session_id,
        }

    def active_step_title(self) -> str:
        for step in self.steps:
            if step.status in {"active", "failed", "pending"}:
                return step.title
        return ""


def quest_path(workspace: Path, *, session_id: str = "") -> Path:
    if session_id:
        return workspace / QUESTS_DIR / f"{session_id}.json"
    return workspace / QUEST_REL


def load_quest(workspace: Path, *, session_id: str = "") -> Quest | None:
    path = quest_path(workspace, session_id=session_id)
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
            attempts=int(s.get("attempts") or 0),
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
        session_id=str(raw.get("session_id") or session_id or ""),
    )


def save_quest(
    workspace: Path, quest: Quest | None, *, session_id: str = ""
) -> None:
    sid = session_id or (quest.session_id if quest is not None else "")
    path = quest_path(workspace, session_id=sid)
    if quest is None:
        path.unlink(missing_ok=True)
        return
    if sid and not quest.session_id:
        quest.session_id = sid
    path.parent.mkdir(parents=True, exist_ok=True)
    quest.updated_at = time.time()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(quest.public(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp.replace(path)


def start_quest(
    workspace: Path, goal: str, steps: list[str], *, session_id: str = ""
) -> Quest:
    quest = Quest(
        id=uuid.uuid4().hex[:8],
        goal=goal.strip(),
        status="active",
        session_id=session_id,
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
        quest.steps = [
            QuestStep(id=uuid.uuid4().hex[:6], title="执行目标", status="active")
        ]
    save_quest(workspace, quest, session_id=session_id)
    return quest


def set_step_status(
    workspace: Path,
    step_id: str,
    status: StepStatus,
    *,
    note: str = "",
    blocked_reason: str = "",
    session_id: str = "",
) -> Quest | None:
    quest = load_quest(workspace, session_id=session_id)
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
    save_quest(workspace, quest, session_id=session_id)
    return quest


def resume_quest(workspace: Path, *, session_id: str = "") -> Quest | None:
    """Clear blocked state and focus the first failed/pending step."""
    quest = load_quest(workspace, session_id=session_id)
    if quest is None:
        return None
    for step in quest.steps:
        if step.status == "failed":
            step.status = "active"
            step.note = ""
            step.attempts = 0
            break
        if step.status == "pending":
            step.status = "active"
            break
        if step.status == "active":
            break
    quest.status = "active"
    quest.blocked_reason = ""
    save_quest(workspace, quest, session_id=session_id)
    return quest


def quest_prompt_hint(workspace: Path, *, session_id: str = "") -> str:
    quest = load_quest(workspace, session_id=session_id)
    if quest is None or quest.status in {"idle", "done"}:
        return ""
    active = quest.active_step_title() or "(无步骤)"
    blocked = f"\nBlocked: {quest.blocked_reason}" if quest.blocked_reason else ""
    return (
        f"[Active Quest]\nGoal: {quest.goal}\nCurrent step: {active}"
        f"{blocked}\nContinue from this step; do not restart completed work.\n\n"
    )


def sync_quest_from_todos(
    workspace: Path, todos: list[dict[str, Any]], *, session_id: str = ""
) -> Quest | None:
    """Mirror TodoWrite into an active Quest (create steps if needed)."""
    quest = load_quest(workspace, session_id=session_id)
    if quest is None or quest.status in {"idle", "done"}:
        return None
    if not todos:
        return quest
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
        save_quest(workspace, quest, session_id=session_id)
        return quest
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
    save_quest(workspace, quest, session_id=session_id)
    return quest


def sync_quest_from_verify(
    workspace: Path,
    *,
    verdict: str,
    failures: int = 0,
    session_id: str = "",
) -> Quest | None:
    """Mark the active Quest step failed/done from a Verify result."""
    quest = load_quest(workspace, session_id=session_id)
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
        active.attempts += 1
        if active.attempts <= QUEST_STEP_MAX_RETRIES:
            active.status = "active"
            active.note = (
                f"Verify 失败（第 {active.attempts}/{QUEST_STEP_MAX_RETRIES} 次），"
                f"自动重试：{verdict}"
            )
            quest.status = "active"
            quest.blocked_reason = ""
            quest.retry_pending = True
            save_quest(workspace, quest, session_id=session_id)
            return quest
        active.status = "failed"
        active.note = verdict
        quest.status = "blocked"
        quest.blocked_reason = f"Verify: {verdict}"
        save_quest(workspace, quest, session_id=session_id)
        return quest
    if "PASS" in upper:
        return set_step_status(
            workspace, active.id, "done", note=verdict, session_id=session_id
        )
    return quest
