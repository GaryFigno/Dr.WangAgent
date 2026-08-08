"""Filesystem tools: Read, Write, Edit, Glob, Grep."""

from __future__ import annotations

import asyncio
import fnmatch
import re
import shutil
from pathlib import Path
from typing import Any

from ..constants import (
    BINARY_PRINTABLE_THRESHOLD,
    BINARY_SNIFF_BYTES,
    DIRECTORY_LISTING_LIMIT,
    GLOB_RESULT_LIMIT,
    GLOB_SCAN_MULTIPLIER,
    GREP_RESULT_LIMIT,
    GREP_SUBPROCESS_TIMEOUT,
    MAX_LINE_CHARS,
    MAX_READ_LINES,
    PATH_SUGGEST_LIMIT,
    PATH_SUGGEST_PREFIX_CHARS,
)
from ..process import hidden_subprocess_kwargs
from ..workspace.ignore import (
    DEFAULT_IGNORED_DIR_NAMES,
    IgnoreMatcher,
    IGNORED_DIRS,  # noqa: F401 — re-export for older imports
)
from .base import Tool, ToolContext, ToolResult

#: Bytes that count as printable when sniffing for binary content.
PRINTABLE_ASCII = range(32, 127)
PRINTABLE_CONTROL = (9, 10, 13)  # tab, newline, carriage return

#: Encodings tried, in order, when reading a text file.
TEXT_ENCODINGS = ("utf-8", "utf-8-sig", "gbk", "latin-1")

# Back-compat alias (set → frozenset).
IGNORED_DIRS = DEFAULT_IGNORED_DIR_NAMES


def _queue_review(
    ctx: ToolContext,
    *,
    path: Path,
    kind: str,
    before: str | None,
    after: str,
    old: str = "",
    new: str = "",
    line: int | None = None,
    added: int = 0,
    removed: int = 0,
    created: bool = False,
) -> None:
    """Register a disk write for the GUI Apply/Reject board when present."""
    board = ctx.edit_review
    if board is None:
        return
    board.add(
        path=path,
        rel=ctx.rel(path),
        kind=kind,  # type: ignore[arg-type]
        before=before,
        after=after,
        old=old,
        new=new,
        line=line,
        added=added,
        removed=removed,
        call_id=ctx.current_call_id,
        created=created,
    )


def _looks_binary(path: Path) -> bool:
    """Guess whether a file is binary by sampling its first bytes."""
    try:
        with path.open("rb") as handle:
            chunk = handle.read(BINARY_SNIFF_BYTES)
    except OSError:
        return False
    if b"\x00" in chunk:
        return True
    if not chunk:
        return False
    printable = sum(
        1 for byte in chunk if byte in PRINTABLE_ASCII or byte in PRINTABLE_CONTROL
    )
    return printable / len(chunk) < BINARY_PRINTABLE_THRESHOLD


def _read_text(path: Path) -> str:
    """Read a text file, trying several encodings before giving up.

    Raises:
      OSError: If the file cannot be opened at all.
    """
    for encoding in TEXT_ENCODINGS:
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
        except OSError as error:
            raise OSError(f"cannot read {path}: {error}") from error
    return path.read_text(encoding="utf-8", errors="replace")


class ReadTool(Tool):
    name = "Read"
    bulky = True
    description = """
Read a file from the filesystem. Returns lines numbered `N\tcontent`,
so you can quote exact line numbers back to the user.
Use offset/limit for files too large to read at once.
Always Read a file before editing it.
"""

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to the file (absolute or workspace-relative)"},
                "offset": {"type": "integer", "description": "First line to read (1-indexed)"},
                "limit": {"type": "integer", "description": "How many lines to read"},
                "force": {
                    "type": "boolean",
                    "description": "Re-read even when the file is unchanged since the last Read",
                },
            },
            "required": ["file_path"],
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = ctx.resolve(str(args["file_path"]))
        if not path.exists():
            near = _suggest_near(path)
            hint = f"\nDid you mean: {near}" if near else ""
            return ToolResult.error(f"File not found: {path}{hint}")
        if path.is_dir():
            entries = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())
            listing = "\n".join(entries[:DIRECTORY_LISTING_LIMIT])
            return ToolResult(
                content=f"{path} is a directory. Contents:\n{listing}",
                summary=f"listed {len(entries)} entries",
            )
        if _looks_binary(path):
            size = path.stat().st_size
            return ToolResult.error(f"{path} looks like a binary file ({size} bytes); not read.")

        key = str(path.resolve())
        try:
            mtime = path.stat().st_mtime
        except OSError as error:
            return ToolResult.error(str(error))
        offset = max(int(args.get("offset") or 1), 1)
        limit = int(args.get("limit") or MAX_READ_LINES)
        force = bool(args.get("force"))
        # OpenCode-style: unchanged re-reads become a stub unless forced /
        # windowed, so the main model is not fed the same file twice.
        if (
            not force
            and offset == 1
            and limit >= MAX_READ_LINES
            and ctx.read_files.get(key) == mtime
        ):
            return ToolResult(
                content=(
                    f"[unchanged] {path} — same mtime as the last Read; "
                    "full contents omitted. Pass force=true or a new offset "
                    "to re-read."
                ),
                summary=f"cached {ctx.rel(path)}",
                display={"kind": "read", "path": str(path), "cached": True},
            )

        text = await asyncio.to_thread(_read_text, path)
        lines = text.splitlines()
        window = lines[offset - 1 : offset - 1 + limit]

        rendered = []
        for number, raw_line in enumerate(window, start=offset):
            line = raw_line
            if len(line) > MAX_LINE_CHARS:
                overflow = len(line) - MAX_LINE_CHARS
                line = line[:MAX_LINE_CHARS] + f"… [{overflow} more chars]"
            rendered.append(f"{number}\t{line}")

        ctx.read_files[key] = mtime

        body = "\n".join(rendered)
        if not body:
            body = "(file is empty)" if not lines else f"(no lines at offset {offset})"
        trailer = ""
        end = offset - 1 + len(window)
        if end < len(lines):
            trailer = f"\n\n[{len(lines) - end} more lines; re-read with offset={end + 1}]"
        return ToolResult(
            content=body + trailer,
            summary=f"read {ctx.rel(path)} ({len(window)} lines)",
            display={"kind": "read", "path": str(path)},
        )


def _suggest_near(path: Path, limit: int = PATH_SUGGEST_LIMIT) -> str:
    """Offer sibling filenames that look like a mistyped target."""
    parent = path.parent
    if not parent.is_dir():
        return ""
    prefix = path.name.lower()[:PATH_SUGGEST_PREFIX_CHARS]
    if not prefix:
        return ""
    candidates = [entry.name for entry in parent.iterdir() if prefix in entry.name.lower()]
    return ", ".join(candidates[:limit])


class WriteTool(Tool):
    name = "Write"
    description = """
Write a file, creating parent directories as needed and overwriting any
existing content. Prefer Edit for partial changes to an existing file.
"""

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["file_path", "content"],
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = ctx.resolve(str(args["file_path"]))
        content = str(args.get("content", ""))
        existed = path.exists()

        if existed and _looks_binary(path):
            return ToolResult.error(f"refusing to overwrite binary file {path}")

        before: str | None = None
        if existed:
            before = await asyncio.to_thread(_read_text, path)

        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_text, content, encoding="utf-8")
        ctx.read_files[str(path.resolve())] = path.stat().st_mtime

        n_lines = content.count("\n") + 1
        verb = "overwrote" if existed else "created"
        _queue_review(
            ctx,
            path=path,
            kind="write",
            before=before,
            after=content,
            new=content,
            added=n_lines,
            removed=(before.count("\n") + 1) if before is not None else 0,
            created=not existed,
        )
        return ToolResult(
            content=f"{verb} {path} ({n_lines} lines, {len(content)} chars)",
            summary=f"{verb} {ctx.rel(path)}",
            display={"kind": "write", "path": str(path), "lines": n_lines},
        )


class EditTool(Tool):
    name = "Edit"
    description = """
Replace an exact string in a file. `old_string` must appear exactly once
unless `replace_all` is true, and must match the file byte for byte
(including indentation, without the line-number prefix Read adds).
Read the file first — editing an unread file is refused.
"""

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "replace_all": {"type": "boolean", "description": "Replace every occurrence"},
            },
            "required": ["file_path", "old_string", "new_string"],
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = ctx.resolve(str(args["file_path"]))
        old = str(args["old_string"])
        new = str(args["new_string"])
        replace_all = bool(args.get("replace_all"))

        if not path.exists():
            return ToolResult.error(f"File not found: {path}")
        key = str(path.resolve())
        if key not in ctx.read_files:
            return ToolResult.error(
                f"Read {ctx.rel(path)} before editing it, so you are editing the current content."
            )
        if old == new:
            return ToolResult.error("old_string and new_string are identical")

        text = await asyncio.to_thread(_read_text, path)
        count = text.count(old)
        if count == 0:
            hint = ""
            stripped = old.strip()
            if stripped and stripped in text:
                hint = " (a whitespace-insensitive match exists — check indentation)"
            return ToolResult.error(f"old_string not found in {ctx.rel(path)}{hint}")
        if count > 1 and not replace_all:
            return ToolResult.error(
                f"old_string appears {count} times in {ctx.rel(path)}; "
                f"add surrounding context to make it unique, or pass replace_all=true"
            )

        updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        await asyncio.to_thread(path.write_text, updated, encoding="utf-8")
        ctx.read_files[key] = path.stat().st_mtime

        line_no = text[: text.find(old)].count("\n") + 1
        removed = old.count("\n") + (1 if old else 0)
        added = new.count("\n") + (1 if new else 0)
        if replace_all and count > 1:
            removed *= count
            added *= count
        _queue_review(
            ctx,
            path=path,
            kind="edit",
            before=text,
            after=updated,
            old=old,
            new=new,
            line=line_no,
            added=added,
            removed=removed,
            created=False,
        )
        return ToolResult(
            content=f"edited {path} at line {line_no} ({count if replace_all else 1} replacement(s))",
            summary=f"edited {ctx.rel(path)}:{line_no} +{added} -{removed}",
            display={
                "kind": "edit",
                "path": str(path),
                "line": line_no,
                "old": old,
                "new": new,
                "added": added,
                "removed": removed,
            },
        )


class GlobTool(Tool):
    name = "Glob"
    description = """
Find files by glob pattern (e.g. `src/**/*.py`). Results are sorted by
modification time, newest first. Fast on large trees.
"""

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string", "description": "Directory to search in; defaults to the workspace"},
                "limit": {"type": "integer"},
            },
            "required": ["pattern"],
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        root = ctx.resolve(str(args.get("path") or "."))
        pattern = str(args["pattern"])
        limit = int(args.get("limit") or GLOB_RESULT_LIMIT)
        scan_ceiling = limit * GLOB_SCAN_MULTIPLIER

        def walk() -> list[Path]:
            matcher = IgnoreMatcher.for_workspace(ctx.workspace)
            found: list[Path] = []
            for p in root.rglob("*"):
                if len(found) >= scan_ceiling:
                    break
                if matcher.is_ignored(p):
                    continue
                if not p.is_file():
                    continue
                rel = p.relative_to(root).as_posix()
                if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(p.name, pattern):
                    found.append(p)
            return found

        try:
            matches = await asyncio.to_thread(walk)
        except OSError as e:
            return ToolResult.error(f"glob failed: {e}")

        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        shown = matches[:limit]
        if not shown:
            return ToolResult(content=f"No files match {pattern} under {root}", summary="0 matches")
        body = "\n".join(str(p) for p in shown)
        more = f"\n\n[{len(matches) - len(shown)} more]" if len(matches) > len(shown) else ""
        return ToolResult(content=body + more, summary=f"{len(matches)} match(es) for {pattern}")


class GrepTool(Tool):
    name = "Grep"
    bulky = True
    description = """
Search file contents with a regular expression. Uses ripgrep when it is on
PATH and falls back to a pure-Python scan otherwise.
output_mode: "content" (matching lines), "files" (paths only), "count".
"""

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regular expression"},
                "path": {"type": "string"},
                "glob": {"type": "string", "description": "Restrict to files matching this glob"},
                "output_mode": {"type": "string", "enum": ["content", "files", "count"]},
                "case_insensitive": {"type": "boolean"},
                "context": {"type": "integer", "description": "Lines of context around each match"},
                "limit": {"type": "integer"},
            },
            "required": ["pattern"],
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        pattern = str(args["pattern"])
        root = ctx.resolve(str(args.get("path") or "."))
        mode = str(args.get("output_mode") or "files")
        limit = int(args.get("limit") or GREP_RESULT_LIMIT)

        if shutil.which("rg"):
            result = await self._ripgrep(pattern, root, args, mode, limit)
            if result is not None:
                return result
        return await asyncio.to_thread(self._python_grep, pattern, root, args, mode, limit, ctx)

    async def _ripgrep(
        self, pattern: str, root: Path, args: dict[str, Any], mode: str, limit: int
    ) -> ToolResult | None:
        cmd = ["rg", "--no-heading", "--color=never"]
        if mode == "files":
            cmd.append("--files-with-matches")
        elif mode == "count":
            cmd.append("--count")
        else:
            cmd.append("--line-number")
            if args.get("context"):
                cmd += ["-C", str(int(args["context"]))]
        if args.get("case_insensitive"):
            cmd.append("-i")
        if args.get("glob"):
            cmd += ["--glob", str(args["glob"])]
        cmd += ["--max-count", str(limit), "-e", pattern, str(root)]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **hidden_subprocess_kwargs(),
            )
            out, _ = await asyncio.wait_for(
                proc.communicate(), timeout=GREP_SUBPROCESS_TIMEOUT
            )
        except (OSError, asyncio.TimeoutError):
            return None

        if proc.returncode not in (0, 1):
            return None
        text = out.decode("utf-8", "replace").strip()
        if not text:
            return ToolResult(content=f"No matches for /{pattern}/ under {root}", summary="0 matches")
        lines = text.splitlines()
        shown = lines[:limit]
        more = f"\n\n[{len(lines) - len(shown)} more lines]" if len(lines) > len(shown) else ""
        return ToolResult(
            content="\n".join(shown) + more,
            summary=f"{len(lines)} result line(s) for /{pattern}/",
        )

    def _python_grep(
        self,
        pattern: str,
        root: Path,
        args: dict[str, Any],
        mode: str,
        limit: int,
        ctx: ToolContext,
    ) -> ToolResult:
        flags = re.IGNORECASE if args.get("case_insensitive") else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as e:
            return ToolResult.error(f"invalid regex: {e}")

        glob_filter = args.get("glob")
        context = int(args.get("context") or 0)
        results: list[str] = []
        files_with_matches: list[str] = []
        counts: dict[str, int] = {}

        matcher = IgnoreMatcher.for_workspace(ctx.workspace)
        paths = [root] if root.is_file() else root.rglob("*")
        for p in paths:
            if len(results) >= limit and mode == "content":
                break
            if matcher.is_ignored(p) or not p.is_file():
                continue
            if glob_filter and not (
                fnmatch.fnmatch(p.name, glob_filter)
                or fnmatch.fnmatch(p.as_posix(), glob_filter)
            ):
                continue
            if _looks_binary(p):
                continue
            try:
                lines = _read_text(p).splitlines()
            except OSError:
                continue

            hits = [i for i, line in enumerate(lines) if regex.search(line)]
            if not hits:
                continue
            files_with_matches.append(str(p))
            counts[str(p)] = len(hits)
            if mode == "content":
                for i in hits:
                    lo, hi = max(i - context, 0), min(i + context + 1, len(lines))
                    for j in range(lo, hi):
                        marker = ":" if j == i else "-"
                        results.append(f"{p}{marker}{j + 1}{marker}{lines[j][:MAX_LINE_CHARS]}")
                    if len(results) >= limit:
                        break

        if mode == "files":
            if not files_with_matches:
                return ToolResult(content=f"No matches for /{pattern}/", summary="0 matches")
            return ToolResult(
                content="\n".join(files_with_matches[:limit]),
                summary=f"{len(files_with_matches)} file(s) matched",
            )
        if mode == "count":
            if not counts:
                return ToolResult(content=f"No matches for /{pattern}/", summary="0 matches")
            body = "\n".join(f"{path}: {n}" for path, n in sorted(counts.items()))
            return ToolResult(content=body, summary=f"{sum(counts.values())} match(es)")
        if not results:
            return ToolResult(content=f"No matches for /{pattern}/", summary="0 matches")
        return ToolResult(
            content="\n".join(results[:limit]),
            summary=f"{len(results)} matching line(s)",
        )
