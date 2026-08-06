"""User interface preferences.

Kept in a separate file from ``config.yaml`` on purpose: ``/theme`` and
``/pet`` write on every change, and rewriting a hand-edited YAML config —
comments and all — every time somebody tries a colour scheme is a good way to
destroy their configuration.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from platformdirs import user_config_dir

from .theme import DEFAULT_THEME

PREFS_FILE = "ui.json"


def prefs_path() -> Path:
    override = os.environ.get("AIH_PREFS_FILE")
    if override:
        return Path(override).expanduser()
    return Path(user_config_dir("aiharness", appauthor=False)) / PREFS_FILE


@dataclass
class UIPrefs:
    """Everything the user can change from inside the app."""

    theme: str = DEFAULT_THEME
    #: Show the mascot in the corner.
    pet: bool = True
    #: Mascot rendering: "cat" (drawn), "emoji" (single glyph), "off".
    pet_style: str = "cat"
    show_reasoning: bool = True
    show_cost: bool = True
    #: Show a divider in the transcript wherever the context was compacted.
    show_compaction_markers: bool = True
    #: Collapse tool output to a single line until clicked.
    collapse_tools: bool = True
    sidebar_visible: bool = False
    #: UI language: ``auto`` or a code from :mod:`aiharness.gui.locale`
    #: (zh / en / ja / ko / …). Used by the desktop GUI and TUI labels.
    language: str = "auto"

    @classmethod
    def load(cls, path: Path | None = None) -> UIPrefs:
        target = path or prefs_path()
        if not target.is_file():
            return cls()
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        known = {k: v for k, v in payload.items() if k in cls.__annotations__}
        try:
            return cls(**known)
        except TypeError:
            return cls()

    def save(self, path: Path | None = None) -> None:
        target = path or prefs_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8"
        )
