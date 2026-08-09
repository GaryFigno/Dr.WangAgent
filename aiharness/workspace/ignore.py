"""Workspace ignore rules (hardcoded dirs + .gitignore + .aiharnessignore).

Mirrors how Claude Code / Aider / ripgrep skip junk when indexing, without
pulling in an extra dependency. Nested ``.gitignore`` files are loaded when
encountered during walks that use :meth:`IgnoreMatcher.skip_dir`.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..process import hidden_subprocess_kwargs

#: Always skipped directory names (even without a .gitignore).
DEFAULT_IGNORED_DIR_NAMES = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".next",
        ".turbo",
        "target",
        ".idea",
        ".gradle",
        ".tox",
        ".cache",
    }
)

#: Dot-directories that the file tree still shows.
TREE_VISIBLE_DOT_DIRS = frozenset({".aiharness", ".github"})

_IGNORE_FILE_NAMES = (".gitignore", ".aiharnessignore")
_GIT_LS_TIMEOUT = 45.0
_matcher_cache: dict[str, tuple[float, "IgnoreMatcher"]] = {}


@dataclass
class _Pattern:
    raw: str
    regex: re.Pattern[str]
    negate: bool
    dir_only: bool


def _gitignore_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile one gitignore pattern to a regex matching relative posix paths."""
    anchored = pattern.startswith("/")
    if anchored:
        pattern = pattern[1:]
    parts: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            parts.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            parts.append(".*")
            i += 2
            continue
        ch = pattern[i]
        if ch == "*":
            parts.append("[^/]*")
        elif ch == "?":
            parts.append("[^/]")
        elif ch == "/":
            parts.append("/")
        else:
            parts.append(re.escape(ch))
        i += 1
    body = "".join(parts)
    if anchored:
        return re.compile(f"^{body}(?:/.*)?$")
    return re.compile(f"(?:^|/){body}(?:/.*)?$")


def _parse_ignore_text(text: str) -> list[_Pattern]:
    patterns: list[_Pattern] = []
    for line in text.splitlines():
        raw = line.rstrip("\n")
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        negate = raw.startswith("!")
        if negate:
            raw = raw[1:]
        dir_only = raw.endswith("/")
        if dir_only:
            raw = raw[:-1]
        raw = raw.strip()
        if not raw:
            continue
        try:
            patterns.append(
                _Pattern(
                    raw=raw,
                    regex=_gitignore_to_regex(raw),
                    negate=negate,
                    dir_only=dir_only,
                )
            )
        except re.error:
            continue
    return patterns


@dataclass
class IgnoreMatcher:
    """Ignore decisions for one workspace root."""

    root: Path
    dir_names: frozenset[str] = DEFAULT_IGNORED_DIR_NAMES
    patterns: list[_Pattern] = field(default_factory=list)

    @classmethod
    def for_workspace(cls, workspace: Path) -> IgnoreMatcher:
        root = workspace.resolve()
        stamp = _ignore_stamp(root)
        key = str(root)
        cached = _matcher_cache.get(key)
        if cached and cached[0] == stamp:
            return cached[1]
        patterns: list[_Pattern] = []
        for name in _IGNORE_FILE_NAMES:
            path = root / name
            if not path.is_file():
                continue
            try:
                patterns.extend(_parse_ignore_text(path.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                continue
        matcher = cls(root=root, patterns=patterns)
        _matcher_cache[key] = (stamp, matcher)
        return matcher

    def is_ignored(self, path: Path) -> bool:
        """Whether ``path`` should be skipped for indexing / Glob / search."""
        try:
            resolved = path.resolve()
            rel = resolved.relative_to(self.root).as_posix()
        except (ValueError, OSError):
            # Outside workspace or broken path — treat as ignored for safety.
            return True
        if not rel or rel == ".":
            return False
        parts = Path(rel).parts
        if any(part in self.dir_names for part in parts):
            return True
        is_dir = path.is_dir()
        ignored = False
        for pattern in self.patterns:
            if pattern.dir_only and not is_dir and "/" not in rel:
                # dir-only patterns still match path prefixes via regex `/.*`
                pass
            if pattern.regex.search(rel):
                ignored = not pattern.negate
        return ignored

    def skip_dir(self, dir_path: Path) -> bool:
        """True when a walk should not descend into ``dir_path``."""
        name = dir_path.name
        if name in self.dir_names:
            return True
        return self.is_ignored(dir_path)


def _ignore_stamp(root: Path) -> float:
    stamp = 0.0
    try:
        stamp = root.stat().st_mtime
    except OSError:
        return 0.0
    for name in _IGNORE_FILE_NAMES:
        try:
            stamp = max(stamp, (root / name).stat().st_mtime)
        except OSError:
            continue
    return stamp


def invalidate_ignore_cache(workspace: Path | None = None) -> None:
    if workspace is None:
        _matcher_cache.clear()
        return
    _matcher_cache.pop(str(workspace.resolve()), None)


def git_tracked_and_visible_files(workspace: Path) -> list[str] | None:
    """Return relative paths from ``git ls-files -co --exclude-standard``.

    ``None`` means git is unavailable; callers should fall back to a walk.
    """
    root = workspace.resolve()
    if not (root / ".git").exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=_GIT_LS_TIMEOUT,
            check=False,
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return [
        line.replace("\\", "/")
        for line in (proc.stdout or "").splitlines()
        if line.strip()
    ]


def path_ignored(workspace: Path, path: Path) -> bool:
    """Convenience wrapper used by tools and indexers."""
    return IgnoreMatcher.for_workspace(workspace).is_ignored(path)


# Back-compat name used across the package before IgnoreMatcher existed.
IGNORED_DIRS = DEFAULT_IGNORED_DIR_NAMES
