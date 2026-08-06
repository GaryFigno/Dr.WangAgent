"""User and project rules injected into the system prompt.

Rules are cache-stable: they change only when files on disk change, not per
turn. Global rules live under the user config directory; project rules live
under ``.aiharness/rules/`` in the workspace.
"""

from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_config_dir

from .constants import APP_SLUG

PROJECT_RULES_DIR = ".aiharness/rules"
#: Max characters loaded from one rule file.
RULE_FILE_MAX_CHARS = 20_000
#: Max total characters from all rule files combined.
RULES_TOTAL_MAX_CHARS = 60_000


def global_rules_dir() -> Path:
    override = os.environ.get("AIH_RULES_DIR")
    if override:
        return Path(override).expanduser()
    return Path(user_config_dir(APP_SLUG, appauthor=False)) / "rules"


def _read_rule_file(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""
    if len(text) > RULE_FILE_MAX_CHARS:
        text = text[:RULE_FILE_MAX_CHARS] + "\n… [truncated]"
    return text


def _iter_rule_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    files = [
        path
        for path in sorted(directory.iterdir())
        if path.is_file() and path.suffix.lower() in {".md", ".txt"}
    ]
    return files


def load_rules(workspace: Path) -> tuple[str, list[str]]:
    """Load global + project rules.

    Returns:
      A ``(section, sources)`` pair. ``section`` is empty when nothing is
      found; ``sources`` lists human-readable origins for the GUI.
    """
    chunks: list[str] = []
    sources: list[str] = []
    total = 0

    for path in _iter_rule_files(global_rules_dir()):
        body = _read_rule_file(path)
        if not body:
            continue
        if total + len(body) > RULES_TOTAL_MAX_CHARS:
            break
        chunks.append(f"### Global rule · {path.name}\n\n{body}")
        sources.append(f"global:{path.name}")
        total += len(body)

    project_dir = workspace / PROJECT_RULES_DIR
    for path in _iter_rule_files(project_dir):
        body = _read_rule_file(path)
        if not body:
            continue
        if total + len(body) > RULES_TOTAL_MAX_CHARS:
            break
        chunks.append(f"### Project rule · {path.name}\n\n{body}")
        sources.append(f"project:{path.name}")
        total += len(body)

    if not chunks:
        return "", []
    section = (
        "# Rules\n\n"
        "These user/project rules override general habits when they conflict.\n\n"
        + "\n\n".join(chunks)
    )
    return section, sources


def write_rule(scope: str, name: str, body: str, workspace: Path) -> Path:
    """Create or overwrite one rule file.

    Args:
      scope: ``global`` or ``project``.
      name: File stem or filename (``.md`` added when missing).
      body: Rule text.
      workspace: Project root (used when scope is project).
    """
    filename = name.strip()
    if not filename:
        raise ValueError("rule name required")
    if not filename.lower().endswith((".md", ".txt")):
        filename += ".md"
    if "/" in filename or "\\" in filename or ".." in filename:
        raise ValueError("rule name must be a plain filename")
    directory = global_rules_dir() if scope == "global" else workspace / PROJECT_RULES_DIR
    if scope not in {"global", "project"}:
        raise ValueError("scope must be global or project")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(body.strip() + "\n", encoding="utf-8")
    return path


def delete_rule(scope: str, name: str, workspace: Path) -> bool:
    filename = name.strip()
    if not filename.lower().endswith((".md", ".txt")):
        filename += ".md"
    directory = global_rules_dir() if scope == "global" else workspace / PROJECT_RULES_DIR
    path = directory / filename
    if not path.is_file():
        return False
    path.unlink()
    return True


def list_rules(workspace: Path) -> list[dict[str, str]]:
    """List editable rules for the GUI."""
    rows: list[dict[str, str]] = []
    for path in _iter_rule_files(global_rules_dir()):
        rows.append(
            {
                "scope": "global",
                "name": path.name,
                "body": _read_rule_file(path),
            }
        )
    for path in _iter_rule_files(workspace / PROJECT_RULES_DIR):
        rows.append(
            {
                "scope": "project",
                "name": path.name,
                "body": _read_rule_file(path),
            }
        )
    return rows
