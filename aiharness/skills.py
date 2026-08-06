"""Skill discovery and loading.

A skill is a folder containing ``SKILL.md`` with YAML frontmatter:

    ---
    name: pdf-forms
    description: Fill and extract PDF form fields. Use when the user mentions
      a .pdf form, AcroForm, or form flattening.
    allowed-tools: [Read, Write, Bash]      # optional
    ---

    # Instructions the model reads only when the skill is invoked
    ...

Only ``name`` and ``description`` enter the system prompt; the body is
loaded on demand by the ``Skill`` tool.  That keeps a large library of
skills nearly free in context.

Claude Code's ``.claude/skills`` layout is read as-is, so existing skills
work without modification.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from platformdirs import user_config_dir

from .constants import (
    SKILL_BUNDLED_FILE_LIMIT,
    SKILL_MAX_BODY_CHARS,
    SKILL_MAX_DESCRIPTION_CHARS,
)

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: Path
    allowed_tools: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    source: str = ""  # which root it came from, for /skills output

    @property
    def directory(self) -> Path:
        return self.path.parent

    def render(self) -> str:
        """The text handed to the model when the skill is invoked."""
        body = self.body
        if len(body) > SKILL_MAX_BODY_CHARS:
            body = body[:SKILL_MAX_BODY_CHARS] + "\n\n[skill body truncated]"
        extras = _list_bundled_files(self.directory)
        trailer = ""
        if extras:
            trailer = (
                "\n\n---\nFiles bundled with this skill (read them with the Read tool "
                "if the instructions refer to them):\n"
                + "\n".join(f"  {p}" for p in extras)
            )
        return f"# Skill: {self.name}\n\n{body}{trailer}"


def _list_bundled_files(directory: Path, limit: int = SKILL_BUNDLED_FILE_LIMIT) -> list[str]:
    """List the non-SKILL.md files shipped alongside a skill."""
    out: list[str] = []
    for p in sorted(directory.rglob("*")):
        if p.name == "SKILL.md" or not p.is_file():
            continue
        if any(part.startswith(".") for part in p.relative_to(directory).parts):
            continue
        out.append(str(p))
        if len(out) >= limit:
            break
    return out


def read_skill_text(path: Path) -> str | None:
    """Read a SKILL.md, trying the encodings that show up in practice.

    UTF-8 only would silently drop skills written on a Chinese Windows
    machine, where editors still default to GBK. A skill that fails to load
    is invisible — the model simply never learns it exists — so this is worth
    being generous about.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    for encoding in ("utf-8", "utf-8-sig", "gbk", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_skill_file(path: Path, source: str = "") -> Skill | None:
    text = read_skill_text(path)
    if text is None:
        return None

    match = FRONTMATTER_RE.match(text)
    if not match:
        # No frontmatter: fall back to the folder name and the first heading.
        first_line = next(
            (line.strip("# ").strip() for line in text.splitlines() if line.strip()), ""
        )
        return Skill(
            name=path.parent.name,
            description=first_line[:300],
            body=text,
            path=path,
            source=source,
        )

    raw_meta, body = match.group(1), match.group(2)
    try:
        meta = yaml.safe_load(raw_meta) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}

    name = str(meta.get("name") or path.parent.name).strip()
    description = str(meta.get("description") or "").strip()
    if not description:
        return None  # a skill with no description can never be selected

    allowed = meta.get("allowed-tools") or meta.get("allowed_tools") or []
    if isinstance(allowed, str):
        allowed = [t.strip() for t in allowed.split(",") if t.strip()]

    return Skill(
        name=name,
        description=description,
        body=body.strip(),
        path=path,
        allowed_tools=list(allowed),
        metadata=meta,
        source=source,
    )


def user_skill_root() -> Path:
    """The user-level skill directory.

    Named rather than built inline so it can be located, overridden and
    checked. A path that only exists inside a function body is a path no test
    can assert about.
    """
    override = os.environ.get("AIH_SKILL_ROOT")
    if override:
        return Path(override).expanduser()
    return Path(user_config_dir("aiharness", appauthor=False)) / "skills"


def default_skill_roots(workspace: Path) -> list[tuple[Path, str]]:
    home = Path.home()
    return [
        (workspace / ".aiharness" / "skills", "project"),
        (workspace / ".claude" / "skills", "project(claude)"),
        (workspace / "skills", "project"),
        (user_skill_root(), "user"),
        (home / ".claude" / "skills", "user(claude)"),
    ]


class SkillLibrary:
    def __init__(self, workspace: Path, extra_paths: list[str] | None = None):
        self.workspace = workspace
        self.extra_paths = extra_paths or []
        self._skills: dict[str, Skill] = {}
        self.errors: list[str] = []

    def load(self) -> SkillLibrary:
        self._skills.clear()
        self.errors.clear()

        roots = default_skill_roots(self.workspace)
        roots += [(Path(p).expanduser(), "config") for p in self.extra_paths]

        for root, source in roots:
            if not root.is_dir():
                continue
            for skill_file in sorted(root.rglob("SKILL.md")):
                skill = parse_skill_file(skill_file, source=source)
                if skill is None:
                    self.errors.append(f"{skill_file}: missing name/description")
                    continue
                # Later roots do not override earlier (project beats user).
                self._skills.setdefault(skill.name, skill)
        return self

    # -- access -----------------------------------------------------------

    def all(self) -> list[Skill]:
        return sorted(self._skills.values(), key=lambda s: s.name)

    def get(self, name: str) -> Skill | None:
        skill = self._skills.get(name)
        if skill:
            return skill
        # Tolerate case and separator drift in model output.
        norm = name.strip().lower().replace("_", "-")
        for key, value in self._skills.items():
            if key.lower().replace("_", "-") == norm:
                return value
        return None

    def names(self) -> list[str]:
        return sorted(self._skills)

    def prompt_section(self) -> str:
        """The compact listing injected into the system prompt."""
        if not self._skills:
            return ""
        lines = [
            "## Available skills",
            "",
            "Packaged instructions for specific kinds of work. When a task matches one,",
            "call the `Skill` tool with its name FIRST — the full instructions load then,",
            "and you follow them instead of your default approach.",
            "",
        ]
        for skill in self.all():
            description = " ".join(skill.description.split())
            if len(description) > SKILL_MAX_DESCRIPTION_CHARS:
                description = description[:SKILL_MAX_DESCRIPTION_CHARS] + "…"
            lines.append(f"- **{skill.name}**: {description}")
        return "\n".join(lines)
