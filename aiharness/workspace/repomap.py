"""Aider-style ranked file / symbol outline for the environment note."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from ..process import hidden_subprocess_kwargs
from .ignore import IgnoreMatcher, git_tracked_and_visible_files

#: Max characters injected into the environment note.
REPOMAP_MAX_CHARS = 2_800
#: Max files outlined.
REPOMAP_MAX_FILES = 24
#: Symbol lines kept per file.
REPOMAP_SYMBOLS_PER_FILE = 8
#: Bytes read per file when extracting defs.
REPOMAP_FILE_BYTES = 80_000

_DEF_PATTERNS = (
    re.compile(r"^\s*(?:export\s+)?(?:async\s+)?(?:function|class|def|interface|type|struct|enum)\s+(\w+)"),
    re.compile(r"^\s*(?:pub\s+)?(?:fn|struct|enum|trait|impl)\s+(\w+)"),
    re.compile(r"^\s*(?:public|private|protected)?\s*(?:static\s+)?(?:class|interface|enum)\s+(\w+)"),
)


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text or "")}


def _dirty_paths(workspace: Path) -> set[str]:
    if not (workspace / ".git").exists():
        return set()
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    dirty: set[str] = set()
    for line in (proc.stdout or "").splitlines():
        path = line[3:].strip().replace("\\", "/")
        if " -> " in path:
            path = path.split(" -> ", 1)[-1]
        if path:
            dirty.add(path)
    return dirty


def _candidate_files(workspace: Path) -> list[str]:
    git_files = git_tracked_and_visible_files(workspace)
    if git_files is not None:
        return [p for p in git_files if Path(p).suffix.lower() in {
            ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".kt",
            ".cs", ".cpp", ".c", ".h", ".hpp", ".gd", ".swift", ".rb", ".php",
            ".vue", ".svelte", ".md",
        }]
    matcher = IgnoreMatcher.for_workspace(workspace)
    found: list[str] = []
    for path in workspace.rglob("*"):
        if len(found) >= 2_000:
            break
        if not path.is_file() or matcher.is_ignored(path):
            continue
        if path.suffix.lower() not in {
            ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".gd", ".md",
        }:
            continue
        try:
            found.append(path.relative_to(workspace).as_posix())
        except ValueError:
            continue
    return found


def _score_file(rel: str, query_tokens: set[str], dirty: set[str]) -> float:
    score = 0.0
    name = Path(rel).name.lower()
    stem = Path(rel).stem.lower()
    parts = {p.lower() for p in Path(rel).parts}
    for token in query_tokens:
        if token == stem or token in name:
            score += 8
        elif token in parts:
            score += 4
        elif token in rel.lower():
            score += 2
    if rel in dirty or any(rel.startswith(d.rstrip("/") + "/") for d in dirty):
        score += 6
    if rel.endswith(".md") and score == 0:
        score -= 1
    return score


def _symbols(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:REPOMAP_FILE_BYTES]
    except OSError:
        return []
    hits: list[str] = []
    for index, line in enumerate(text.splitlines(), start=1):
        for pattern in _DEF_PATTERNS:
            match = pattern.search(line)
            if match:
                hits.append(f"L{index}:{match.group(0).strip()[:80]}")
                break
        if len(hits) >= REPOMAP_SYMBOLS_PER_FILE:
            break
    return hits


def build_repo_map(
    workspace: Path,
    query: str = "",
    *,
    max_chars: int = REPOMAP_MAX_CHARS,
) -> str:
    """Rank files by query + dirty status and outline a few definitions.

    Empty string when nothing useful was found.
    """
    root = workspace.resolve()
    if not root.is_dir():
        return ""
    query_tokens = _tokens(query)
    dirty = _dirty_paths(root)
    ranked = sorted(
        (
            (_score_file(rel, query_tokens, dirty), rel)
            for rel in _candidate_files(root)
        ),
        key=lambda item: (-item[0], item[1]),
    )
    ranked = [(score, rel) for score, rel in ranked if score > 0 or rel in dirty]
    if not ranked and dirty:
        ranked = [(1.0, rel) for rel in sorted(dirty)[:REPOMAP_MAX_FILES]]
    if not ranked:
        # Cold start: top-level source-ish files.
        ranked = [(0.1, rel) for rel in _candidate_files(root)[:12]]

    lines = ["# Repo map (ranked outline — not full sources)"]
    used = 0
    for _score, rel in ranked[:REPOMAP_MAX_FILES]:
        path = root / rel
        if not path.is_file():
            continue
        block = [f"- {rel}"]
        for sym in _symbols(path):
            block.append(f"  - {sym}")
        chunk = "\n".join(block)
        if used + len(chunk) + 1 > max_chars:
            break
        lines.append(chunk)
        used += len(chunk) + 1
    if len(lines) <= 1:
        return ""
    return "\n".join(lines)
