"""Unsent composer text, keyed by session.

The prompt box used to wipe itself whenever the user switched chats or
restarted the app. Drafts are small and personal, so they live in a single
JSON file under the user config directory rather than inside each session
folder — that keeps the append-only message log clean.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from platformdirs import user_config_dir

from ..constants import COMPOSER_DRAFT_MAX_CHARS

DRAFTS_FILE = "composer_drafts.json"


def drafts_path() -> Path:
    override = os.environ.get("AIH_DRAFTS_FILE")
    if override:
        return Path(override).expanduser()
    return Path(user_config_dir("aiharness", appauthor=False)) / DRAFTS_FILE


class DraftStore:
    """Read and write per-session composer drafts."""

    def __init__(self, path: Path | None = None):
        self.path = path or drafts_path()

    def get(self, session_id: str) -> str:
        if not session_id:
            return ""
        return self._load().get(session_id, "")

    def set(self, session_id: str, text: str) -> None:
        if not session_id:
            return
        trimmed = (text or "")[:COMPOSER_DRAFT_MAX_CHARS]
        data = self._load()
        if not trimmed.strip():
            data.pop(session_id, None)
        else:
            data[session_id] = trimmed
        self._save(data)

    def clear(self, session_id: str) -> None:
        self.set(session_id, "")

    def _load(self) -> dict[str, str]:
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(key): str(value) for key, value in raw.items() if value}

    def _save(self, data: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
