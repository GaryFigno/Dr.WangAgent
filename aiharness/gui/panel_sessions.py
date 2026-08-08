"""Isolated session registry for Codex / Claude GUI panels.

Agent sessions live in :mod:`aiharness.session.store`. Panel sessions are a
separate namespace so Agent / Codex / Claude never share transcript, busy
state, or workspace by accident.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal

from platformdirs import user_data_dir

PanelKind = Literal["codex", "claude"]


def panel_sessions_root(kind: PanelKind) -> Path:
    override = os_environ_root(kind)
    if override is not None:
        return override
    return Path(user_data_dir("aiharness", appauthor=False)) / "panel_sessions" / kind


def os_environ_root(kind: PanelKind) -> Path | None:
    import os

    key = "AIH_CODEX_SESSION_DIR" if kind == "codex" else "AIH_CLAUDE_SESSION_DIR"
    raw = os.environ.get(key, "").strip()
    return Path(raw).expanduser() if raw else None


def _new_id() -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


@dataclass
class PanelSessionMeta:
    id: str
    kind: PanelKind
    workspace: str
    title: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    archived: bool = False
    #: Codex threadId or Claude Code session UUID.
    native_id: str = ""
    profile_id: str = ""
    message_count: int = 0

    @property
    def updated_label(self) -> str:
        age = max(0.0, time.time() - self.updated_at)
        if age < 60:
            return "刚刚"
        if age < 3600:
            return f"{int(age // 60)} 分钟前"
        if age < 86400:
            return f"{int(age // 3600)} 小时前"
        return f"{int(age // 86400)} 天前"

    def public_row(
        self,
        *,
        active: bool,
        running: bool = False,
        waiting: bool = False,
    ) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title or "(未命名)",
            "updated": self.updated_label,
            "messages": self.message_count,
            "cost": 0.0,
            "archived": self.archived,
            "active": active,
            "running": running,
            "waiting": waiting,
            "native_id": self.native_id,
            "workspace": self.workspace,
        }


@dataclass
class PanelTranscriptEntry:
    role: str
    text: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=time.time)

    def public(self) -> dict[str, Any]:
        return {"role": self.role, "text": self.text, "meta": self.meta, "at": self.at}


@dataclass
class PanelSessionStore:
    kind: PanelKind
    root: Path | None = None

    def __post_init__(self) -> None:
        if self.root is None:
            self.root = panel_sessions_root(self.kind)
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, session_id: str) -> Path:
        assert self.root is not None
        return self.root / session_id

    def _meta_path(self, session_id: str) -> Path:
        return self._dir(session_id) / "meta.json"

    def _transcript_path(self, session_id: str) -> Path:
        return self._dir(session_id) / "transcript.jsonl"

    def create(
        self,
        workspace: Path,
        *,
        title: str = "",
        profile_id: str = "",
        native_id: str = "",
        session_id: str = "",
    ) -> PanelSessionMeta:
        sid = (session_id or _new_id()).strip()
        meta = PanelSessionMeta(
            id=sid,
            kind=self.kind,
            workspace=str(workspace),
            title=(title or "").strip() or "新对话",
            profile_id=(profile_id or "").strip(),
            native_id=(native_id or "").strip(),
        )
        self._dir(sid).mkdir(parents=True, exist_ok=True)
        self.save_meta(meta)
        return meta

    def save_meta(self, meta: PanelSessionMeta) -> None:
        path = self._meta_path(meta.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(asdict(meta), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def get(self, session_id: str) -> PanelSessionMeta | None:
        path = self._meta_path(session_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        try:
            return PanelSessionMeta(
                id=str(data.get("id") or session_id),
                kind=self.kind,
                workspace=str(data.get("workspace") or ""),
                title=str(data.get("title") or ""),
                created_at=float(data.get("created_at") or time.time()),
                updated_at=float(data.get("updated_at") or time.time()),
                archived=bool(data.get("archived")),
                native_id=str(data.get("native_id") or ""),
                profile_id=str(data.get("profile_id") or ""),
                message_count=int(data.get("message_count") or 0),
            )
        except (TypeError, ValueError):
            return None

    def list(
        self,
        *,
        workspace: Path | str | None = None,
        include_archived: bool = False,
        keep: str = "",
    ) -> list[PanelSessionMeta]:
        assert self.root is not None
        want = str(workspace) if workspace is not None else ""
        items: list[PanelSessionMeta] = []
        for child in self.root.iterdir():
            if not child.is_dir():
                continue
            meta = self.get(child.name)
            if meta is None:
                continue
            if want and meta.workspace != want and meta.id != keep:
                continue
            if meta.archived and not include_archived and meta.id != keep:
                continue
            items.append(meta)
        items.sort(key=lambda m: m.updated_at, reverse=True)
        return items

    def touch(
        self,
        session_id: str,
        *,
        title: str | None = None,
        native_id: str | None = None,
        profile_id: str | None = None,
        workspace: Path | str | None = None,
        bump_messages: int = 0,
    ) -> PanelSessionMeta | None:
        meta = self.get(session_id)
        if meta is None:
            return None
        meta.updated_at = time.time()
        if title is not None and title.strip():
            meta.title = title.strip()[:80]
        if native_id is not None:
            meta.native_id = native_id.strip()
        if profile_id is not None:
            meta.profile_id = profile_id.strip()
        if workspace is not None:
            meta.workspace = str(workspace)
        if bump_messages:
            meta.message_count = max(0, meta.message_count + bump_messages)
        self.save_meta(meta)
        return meta

    def set_archived(self, session_id: str, archived: bool) -> PanelSessionMeta | None:
        meta = self.get(session_id)
        if meta is None:
            return None
        meta.archived = archived
        meta.updated_at = time.time()
        self.save_meta(meta)
        return meta

    def delete(self, session_id: str) -> bool:
        path = self._dir(session_id)
        if not path.exists():
            return False
        import shutil

        shutil.rmtree(path, ignore_errors=True)
        return True

    def delete_workspace(self, workspace: Path | str) -> int:
        want = str(workspace)
        removed = 0
        for meta in list(self.list(include_archived=True)):
            if meta.workspace == want:
                if self.delete(meta.id):
                    removed += 1
        return removed

    def append_transcript(self, session_id: str, entry: PanelTranscriptEntry) -> None:
        path = self._transcript_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry.public(), ensure_ascii=False) + "\n")
        self.touch(session_id, bump_messages=1 if entry.role in {"user", "assistant"} else 0)

    def load_transcript(self, session_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
        path = self._transcript_path(session_id)
        if not path.is_file():
            return []
        rows: list[dict[str, Any]] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                rows.append(data)
        return rows

    def ui_groups(
        self,
        *,
        viewed_id: str,
        viewed_workspace: str,
        running_ids: Iterable[str] | None = None,
        waiting_ids: Iterable[str] | None = None,
        show_archived: bool = False,
    ) -> dict[str, Any]:
        running = set(running_ids or ())
        waiting = set(waiting_ids or ())
        from .workspace import is_app_install_workspace

        all_metas = self.list(include_archived=True)
        known: dict[str, int] = {}
        for meta in all_metas:
            if meta.archived and not show_archived and meta.id != viewed_id:
                continue
            # Hide phantom groups created when the app launched from its install dir.
            if (
                is_app_install_workspace(meta.workspace)
                and meta.message_count <= 0
                and meta.id != viewed_id
            ):
                continue
            known[meta.workspace] = known.get(meta.workspace, 0) + 1
        if viewed_workspace and not is_app_install_workspace(viewed_workspace):
            known.setdefault(viewed_workspace, 0)

        groups: list[dict[str, Any]] = []
        for path in sorted(known, key=lambda p: (p != viewed_workspace, p.lower())):
            entries = [
                meta
                for meta in all_metas
                if meta.workspace == path
                and (show_archived or not meta.archived or meta.id == viewed_id)
            ]
            entries.sort(key=lambda m: m.updated_at, reverse=True)
            groups.append(
                {
                    "path": path,
                    "name": Path(path).name or path,
                    "active": path == viewed_workspace,
                    "sessions": [
                        meta.public_row(
                            active=meta.id == viewed_id,
                            running=meta.id in running,
                            waiting=meta.id in waiting,
                        )
                        for meta in entries
                    ],
                }
            )
        archived_count = sum(1 for meta in all_metas if meta.archived)
        return {
            "workspaces": groups,
            "show_archived": show_archived,
            "archived_count": archived_count,
            "viewed_id": viewed_id,
        }
