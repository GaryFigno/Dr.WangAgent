"""Write-through edit review: disk is updated, Reject restores a snapshot.

Mutating tools keep writing immediately so mid-turn Read/Grep/Bash stay
consistent. Each successful Edit/Write is queued for the user to Apply
(ack) or Reject (restore ``before``). Same-path stacks reject LIFO.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .diff import preview_for_kind, unified_hunk

EditKind = Literal["edit", "write"]
EditStatus = Literal["pending", "applied", "rejected"]


def _hash_text(text: str | None) -> str:
    if text is None:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass
class PendingEdit:
    id: str
    path: Path
    rel: str
    kind: EditKind
    before: str | None
    after: str
    old: str = ""
    new: str = ""
    line: int | None = None
    added: int = 0
    removed: int = 0
    call_id: str = ""
    created: bool = False
    status: EditStatus = "pending"

    @property
    def before_hash(self) -> str:
        return _hash_text(self.before)

    @property
    def after_hash(self) -> str:
        return _hash_text(self.after)

    def public(self) -> dict[str, Any]:
        """Wire-safe view — full before/after stay server-side."""
        old = self.old if self.kind == "edit" else (self.before or "")
        new = self.new if self.kind == "edit" else self.after
        return {
            "id": self.id,
            "call_id": self.call_id,
            "path": str(self.path),
            "rel": self.rel,
            "kind": self.kind,
            "status": self.status,
            "created": self.created,
            "line": self.line,
            "old": preview_for_kind(self.kind, old),
            "new": preview_for_kind(self.kind, new),
            "unified": unified_hunk(old, new, path=self.rel),
            "added": self.added,
            "removed": self.removed,
            "before_hash": self.before_hash,
            "after_hash": self.after_hash,
        }


@dataclass
class EditReviewBoard:
    """In-memory pending edits for the open GuiSession."""

    items: list[PendingEdit] = field(default_factory=list)

    def clear(self) -> None:
        self.items.clear()

    def add(
        self,
        *,
        path: Path,
        rel: str,
        kind: EditKind,
        before: str | None,
        after: str,
        old: str = "",
        new: str = "",
        line: int | None = None,
        added: int = 0,
        removed: int = 0,
        call_id: str = "",
        created: bool = False,
    ) -> PendingEdit:
        item = PendingEdit(
            id=uuid.uuid4().hex[:10],
            path=path.resolve(),
            rel=rel,
            kind=kind,
            before=before,
            after=after,
            old=old,
            new=new,
            line=line,
            added=added,
            removed=removed,
            call_id=call_id,
            created=created,
        )
        self.items.append(item)
        return item

    def pending(self) -> list[PendingEdit]:
        return [item for item in self.items if item.status == "pending"]

    def public(self) -> list[dict[str, Any]]:
        return [item.public() for item in self.pending()]

    def get(self, edit_id: str) -> PendingEdit | None:
        for item in self.items:
            if item.id == edit_id:
                return item
        return None

    def apply(self, edit_id: str) -> tuple[bool, str]:
        item = self.get(edit_id)
        if item is None:
            return False, "没有这条待审改动"
        if item.status != "pending":
            return False, f"这条改动已经是 {item.status}"
        item.status = "applied"
        return True, f"已接受 {item.rel}"

    def apply_all(self) -> tuple[int, str]:
        count = 0
        for item in self.pending():
            item.status = "applied"
            count += 1
        return count, f"已接受 {count} 处改动"

    def reject(self, edit_id: str) -> tuple[bool, str]:
        item = self.get(edit_id)
        if item is None:
            return False, "没有这条待审改动"
        if item.status != "pending":
            return False, f"这条改动已经是 {item.status}"
        newer = self._newer_pending_on_path(item)
        if newer is not None:
            return False, f"请先处理更新的改动：{newer.rel}（#{newer.id}）"
        ok, detail = self._restore(item)
        if not ok:
            return False, detail
        item.status = "rejected"
        return True, detail

    def reject_all(self) -> tuple[int, list[str]]:
        """Reject newest-first so stacked same-path edits unwind cleanly."""
        remaining = list(reversed(self.pending()))
        done = 0
        errors: list[str] = []
        for item in remaining:
            if item.status != "pending":
                continue
            ok, detail = self.reject(item.id)
            if ok:
                done += 1
            else:
                errors.append(detail)
        return done, errors

    def _newer_pending_on_path(self, item: PendingEdit) -> PendingEdit | None:
        """Newest-first: anything pending on this path before we hit ``item``."""
        for other in reversed(self.items):
            if other is item:
                return None
            if other.status == "pending" and other.path == item.path:
                return other
        return None

    def _restore(self, item: PendingEdit) -> tuple[bool, str]:
        path = item.path
        try:
            if path.exists():
                current = path.read_text(encoding="utf-8")
                if _hash_text(current) != item.after_hash:
                    return (
                        False,
                        f"{item.rel} 已被外部改动，无法安全回滚",
                    )
            elif not item.created:
                return False, f"{item.rel} 已不存在，无法回滚"
        except OSError as error:
            return False, f"读取 {item.rel} 失败：{error}"

        try:
            if item.created:
                if path.exists():
                    path.unlink()
                return True, f"已撤销新建 {item.rel}"
            assert item.before is not None
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(item.before, encoding="utf-8")
            return True, f"已回滚 {item.rel}"
        except OSError as error:
            return False, f"回滚 {item.rel} 失败：{error}"
