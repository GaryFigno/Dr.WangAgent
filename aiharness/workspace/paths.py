"""Local path index for @-mentions and the file tree (no cloud, no embeddings)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ..tools.fs import IGNORED_DIRS

#: Cap on indexed file paths returned to the GUI.
PATH_INDEX_LIMIT = 2000
#: Cap on tree nodes per request.
TREE_NODE_LIMIT = 400
#: Preview body size for @file injection / file panel.
PATH_PREVIEW_CHARS = 6000
#: Directory listing lines shown in a preview.
DIR_PREVIEW_LIMIT = 80
#: Rebuild the full index at most this often unless the stamp changes.
PATH_INDEX_MIN_REBUILD_SECONDS = 2.0
#: A stamp-unchanged index older than this is rebuilt anyway.
PATH_INDEX_MAX_AGE_SECONDS = 60.0

_cache: dict[str, Any] = {"root": "", "stamp": 0.0, "built": 0.0, "entries": []}


def _ignored(path: Path) -> bool:
    return any(part in IGNORED_DIRS for part in path.parts)


def _root_stamp(root: Path) -> float:
    """Cheap invalidation signal: root mtime + shallow child mtimes."""
    try:
        stamp = root.stat().st_mtime
    except OSError:
        return 0.0
    try:
        for entry in root.iterdir():
            if entry.name in IGNORED_DIRS:
                continue
            try:
                stamp = max(stamp, entry.stat().st_mtime)
            except OSError:
                continue
    except OSError:
        pass
    return stamp


def _build_index(root: Path, limit: int) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in root.rglob("*"):
        if len(entries) >= limit:
            break
        if _ignored(path):
            continue
        try:
            rel = path.relative_to(root).as_posix()
            mtime = path.stat().st_mtime
        except (ValueError, OSError):
            continue
        entries.append(
            {
                "path": rel,
                "kind": "dir" if path.is_dir() else "file",
                "mtime": mtime,
            }
        )
    entries.sort(key=lambda item: (-float(item["mtime"]), item["kind"] != "dir", item["path"].lower()))
    return entries


def invalidate_path_index(workspace: Path | None = None) -> None:
    """Drop the cached index (e.g. after a workspace switch)."""
    if workspace is None or _cache.get("root") == str(workspace.resolve()):
        _cache.update(root="", stamp=0.0, built=0.0, entries=[])


def list_paths(
    workspace: Path,
    *,
    query: str = "",
    kind: str = "",
    ext: str = "",
    limit: int = PATH_INDEX_LIMIT,
) -> list[dict[str, Any]]:
    """Return relative paths under ``workspace`` for @ completion.

    Uses an incremental cache keyed by a shallow directory stamp so large
    trees are not fully walked on every keystroke.

    Args:
      kind: Optional ``file`` or ``dir`` filter.
      ext: Optional extension filter such as ``.py`` or ``py``.
    """
    root = workspace.resolve()
    if not root.is_dir():
        return []
    now = time.time()
    stamp = _root_stamp(root)
    root_key = str(root)
    root_changed = _cache.get("root") != root_key
    stamp_changed = _cache.get("stamp") != stamp
    stale = now - float(_cache.get("built") or 0) > PATH_INDEX_MAX_AGE_SECONDS
    # Root/stamp changes must rebuild immediately; the min interval only
    # throttles redundant walks of the same tree under rapid keystrokes.
    if root_changed or stamp_changed:
        _cache["root"] = root_key
        _cache["stamp"] = stamp
        _cache["built"] = now
        _cache["entries"] = _build_index(root, limit)
    elif stale and now - float(_cache.get("built") or 0) >= PATH_INDEX_MIN_REBUILD_SECONDS:
        _cache["stamp"] = stamp
        _cache["built"] = now
        _cache["entries"] = _build_index(root, limit)

    needle = query.strip().lower().lstrip("@")
    kind_filter = kind.strip().lower()
    ext_filter = ext.strip().lower()
    if ext_filter and not ext_filter.startswith("."):
        ext_filter = "." + ext_filter
    hits = []
    for item in _cache.get("entries") or []:
        if kind_filter and item.get("kind") != kind_filter:
            continue
        path = str(item.get("path") or "")
        if ext_filter and item.get("kind") == "file" and not path.lower().endswith(ext_filter):
            continue
        if needle and needle not in path.lower():
            continue
        hits.append(item)
        if len(hits) >= limit:
            break
    return hits


def list_tree(workspace: Path, *, rel: str = "", limit: int = TREE_NODE_LIMIT) -> list[dict[str, Any]]:
    """List one directory level for the file-tree panel."""
    root = workspace.resolve()
    target = (root / rel).resolve() if rel else root
    try:
        target.relative_to(root)
    except ValueError:
        return []
    if not target.is_dir():
        return []
    nodes: list[dict[str, Any]] = []
    try:
        entries = sorted(
            target.iterdir(),
            key=lambda p: (-p.stat().st_mtime if p.exists() else 0, not p.is_dir(), p.name.lower()),
        )
    except OSError:
        return []
    for entry in entries:
        if entry.name in IGNORED_DIRS or entry.name.startswith("."):
            if entry.name not in {".aiharness", ".github"}:
                continue
        if len(nodes) >= limit:
            break
        try:
            child_rel = entry.relative_to(root).as_posix()
            mtime = entry.stat().st_mtime
        except (ValueError, OSError):
            continue
        nodes.append(
            {
                "path": child_rel,
                "name": entry.name,
                "kind": "dir" if entry.is_dir() else "file",
                "mtime": mtime,
            }
        )
    return nodes


def read_path_preview(workspace: Path, rel: str, *, limit: int = PATH_PREVIEW_CHARS) -> dict[str, Any]:
    """Read a short preview for @ injection or the files panel."""
    root = workspace.resolve()
    path = (root / rel).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return {"ok": False, "error": "path outside workspace", "path": rel}
    if not path.exists():
        return {"ok": False, "error": "not found", "path": rel}
    if path.is_dir():
        try:
            names = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())
        except OSError as error:
            return {"ok": False, "error": str(error), "path": rel}
        listing = "\n".join(names[:DIR_PREVIEW_LIMIT])
        return {
            "ok": True,
            "path": rel,
            "kind": "dir",
            "content": listing,
            "truncated": len(names) > DIR_PREVIEW_LIMIT,
        }
    try:
        raw = path.read_bytes()[: limit * 2]
    except OSError as error:
        return {"ok": False, "error": str(error), "path": rel}
    if b"\x00" in raw[:8192]:
        return {"ok": False, "error": "binary file", "path": rel, "kind": "file"}
    text = raw.decode("utf-8", errors="replace")
    truncated = len(text) > limit
    if truncated:
        text = text[:limit] + "\n… [truncated]"
    return {
        "ok": True,
        "path": rel,
        "kind": "file",
        "content": text,
        "truncated": truncated,
    }


def build_refs_block(workspace: Path, refs: list[str]) -> tuple[str, list[str]]:
    """Turn @ paths into a prompt prefix and source labels."""
    if not refs:
        return "", []
    chunks: list[str] = []
    sources: list[str] = []
    for raw in refs:
        rel = str(raw).strip().lstrip("@").replace("\\", "/")
        if not rel:
            continue
        preview = read_path_preview(workspace, rel)
        sources.append(f"ref:{rel}")
        if not preview.get("ok"):
            chunks.append(f"### @{rel}\n\n(unavailable: {preview.get('error', 'error')})")
            continue
        kind = preview.get("kind", "file")
        body = preview.get("content", "")
        label = "directory listing" if kind == "dir" else "file contents"
        chunks.append(f"### @{rel} ({label})\n\n```\n{body}\n```")
    if not chunks:
        return "", []
    block = (
        "[Referenced paths — treat as context the user explicitly attached]\n\n"
        + "\n\n".join(chunks)
        + "\n\n---\n\n"
    )
    return block, sources
