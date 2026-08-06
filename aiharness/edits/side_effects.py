"""Detect workspace file changes after Bash (and similar) for edit review."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from ..tools.fs import IGNORED_DIRS

#: Max files whose contents we snapshot before a Bash call.
SIDE_EFFECT_SNAPSHOT_LIMIT = 120
#: Skip files larger than this when snapshotting.
SIDE_EFFECT_MAX_BYTES = 256_000
#: Max changed files queued into the review board per Bash call.
SIDE_EFFECT_REVIEW_LIMIT = 20


def _ignored(path: Path) -> bool:
    return any(part in IGNORED_DIRS for part in path.parts)


def _file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def snapshot_workspace(workspace: Path) -> dict[str, dict[str, Any]]:
    """Capture small text-ish files under ``workspace`` for later compare."""
    root = workspace.resolve()
    if not root.is_dir():
        return {}
    snaps: dict[str, dict[str, Any]] = {}
    for path in root.rglob("*"):
        if len(snaps) >= SIDE_EFFECT_SNAPSHOT_LIMIT:
            break
        if not path.is_file() or _ignored(path):
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > SIDE_EFFECT_MAX_BYTES:
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:8192]:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        snaps[rel] = {
            "hash": _file_hash(raw),
            "text": text,
            "mtime": path.stat().st_mtime,
        }
    return snaps


def collect_side_effects(
    workspace: Path,
    before: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return changed/new text files since ``before`` (for review queue)."""
    root = workspace.resolve()
    after = snapshot_workspace(root)
    changes: list[dict[str, Any]] = []
    for rel, snap in after.items():
        prev = before.get(rel)
        if prev is None:
            changes.append(
                {
                    "rel": rel,
                    "path": root / rel,
                    "before": None,
                    "after": snap["text"],
                    "created": True,
                }
            )
        elif prev.get("hash") != snap.get("hash"):
            changes.append(
                {
                    "rel": rel,
                    "path": root / rel,
                    "before": prev.get("text"),
                    "after": snap["text"],
                    "created": False,
                }
            )
        if len(changes) >= SIDE_EFFECT_REVIEW_LIMIT:
            break
    return changes


def queue_bash_side_effects(
    ctx: Any,
    before: dict[str, dict[str, Any]],
) -> int:
    """Push Bash-touched files onto the edit-review board. Returns count."""
    board = getattr(ctx, "edit_review", None)
    if board is None or not before:
        return 0
    changes = collect_side_effects(ctx.workspace, before)
    for item in changes:
        board.add(
            path=item["path"],
            rel=item["rel"],
            kind="write",
            before=item["before"],
            after=item["after"],
            old=item["before"] or "",
            new=item["after"] or "",
            created=bool(item.get("created")),
            call_id=getattr(ctx, "current_call_id", "") or "",
        )
    return len(changes)
