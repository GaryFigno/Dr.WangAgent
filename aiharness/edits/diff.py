"""Unified diff helpers for the edit-review UI."""

from __future__ import annotations

import difflib

from ..constants import EDIT_REVIEW_PREVIEW_CHARS, WRITE_REVIEW_PREVIEW_CHARS


def unified_hunk(old: str, new: str, *, path: str = "", limit: int | None = None) -> str:
    """Return a unified diff string, truncated for the wire."""
    a = (old or "").splitlines()
    b = (new or "").splitlines()
    lines = list(
        difflib.unified_diff(
            a,
            b,
            fromfile=f"a/{path}" if path else "a",
            tofile=f"b/{path}" if path else "b",
            lineterm="",
        )
    )
    text = "\n".join(lines) if lines else "(no textual diff)"
    cap = limit if limit is not None else EDIT_REVIEW_PREVIEW_CHARS
    if len(text) > cap:
        return text[: cap - 1] + "…"
    return text


def preview_for_kind(kind: str, text: str) -> str:
    limit = WRITE_REVIEW_PREVIEW_CHARS if kind == "write" else EDIT_REVIEW_PREVIEW_CHARS
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
