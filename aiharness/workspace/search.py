"""Workspace content search for the GUI @ / files panel (no embeddings)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .ignore import IgnoreMatcher

#: Max matching lines returned to the GUI.
SEARCH_HIT_LIMIT = 40
#: Max bytes read per file while searching.
SEARCH_FILE_BYTES = 200_000
#: Max files opened during one search.
SEARCH_SCAN_FILE_LIMIT = 800


def search_content(
    workspace: Path,
    query: str,
    *,
    glob: str = "",
    limit: int = SEARCH_HIT_LIMIT,
) -> list[dict[str, Any]]:
    """Return line hits for ``query`` under ``workspace``.

    ``glob`` is an optional suffix filter such as ``.py`` or ``*.ts``.
    """
    root = workspace.resolve()
    needle = (query or "").strip()
    if not needle or not root.is_dir():
        return []
    try:
        pattern = re.compile(re.escape(needle), re.IGNORECASE)
    except re.error:
        return []

    suffix = ""
    raw_glob = (glob or "").strip()
    if raw_glob.startswith("*."):
        suffix = raw_glob[1:].lower()
    elif raw_glob.startswith("."):
        suffix = raw_glob.lower()
    elif raw_glob:
        suffix = ("." + raw_glob.lstrip(".")).lower()

    hits: list[dict[str, Any]] = []
    scanned = 0
    matcher = IgnoreMatcher.for_workspace(root)
    for path in root.rglob("*"):
        if scanned >= SEARCH_SCAN_FILE_LIMIT or len(hits) >= limit:
            break
        if not path.is_file() or matcher.is_ignored(path):
            continue
        if suffix and not path.name.lower().endswith(suffix):
            continue
        scanned += 1
        try:
            if path.stat().st_size > SEARCH_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "\x00" in text[:4096]:
            continue
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        for index, line in enumerate(text.splitlines(), start=1):
            if not pattern.search(line):
                continue
            hits.append(
                {
                    "path": rel,
                    "line": index,
                    "text": line.strip()[:240],
                    "kind": "content",
                }
            )
            if len(hits) >= limit:
                break
    return hits
