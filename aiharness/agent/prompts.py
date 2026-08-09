"""System prompt construction.

Prompt caching is the reason this module is split the way it is.  Every
backend that caches does so on an exact-prefix basis: the moment one byte of
the system prompt changes, the whole cached prefix is discarded and the turn
is billed at full price.

So the system prompt here contains only things that are stable for the whole
session — instructions, workspace path, platform, project rules, the skill
listing.  Anything volatile (today's date, the current git branch, how many
files are dirty) is built separately by :func:`build_environment_note` and
attached to the *newest* user message, where it costs nothing to change.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from datetime import date
from pathlib import Path

from ..constants import GIT_COMMAND_TIMEOUT
from ..process import hidden_subprocess_kwargs

BASE_PROMPT = """\
You are a coding agent operating directly on the user's machine through tools. \
You read and modify real files and run real commands, so be deliberate: verify \
before you act, and prefer the smallest change that fully solves the problem.

# Working style

- Do the task that was asked. Do not quietly widen the scope, and do not \
narrow it either — if part of the request is blocked, finish everything else \
and say plainly what you left undone and why.
- Investigate before editing. Read the files you are about to change, and look \
at how the surrounding code already does things: match its naming, structure, \
error handling and comment density rather than importing your own habits.
- Never guess at an API, a file path, or a library's behaviour. Check.
- Do not add dependencies without saying so. Do not invent configuration files \
the project does not use.
- When you finish, report what actually happened. If tests fail, say so and \
include the output. Do not claim something works when you have not run it.

# Tools

- Prefer Read/Glob/Grep over shell `cat`/`find`/`grep` — they are faster and \
return cleaner results.
- Read a file before you Edit it. Edit needs an exact byte-for-byte match of \
the existing text, without the line-number prefix that Read adds.
- Issue independent tool calls together in one turn; they run in parallel. \
Only serialise calls when a later one needs an earlier one's result.
- Use TodoWrite for anything with three or more distinct steps, and keep it \
current as you work.
- Paths you pass to tools may be absolute or relative to the workspace.

# Delegating work

You are the expensive model. Hand off work that does not need you:

- `Delegate` runs a self-contained subtask on a cheaper model — mechanical \
edits, file triage, boilerplate, summarising a long file, drafting commit \
messages. Give it complete instructions; it cannot see this conversation.
- `Research` fans several models out over a question in parallel and \
synthesises their findings. Use it for open-ended investigation of an \
unfamiliar codebase or a design question with real trade-offs.
- `Challenge` has an adversarial model attack your proposed solution, then \
triages its findings so you do not act on invented bugs. Use it before \
committing to a design, on anything security-sensitive, and whenever you \
notice you are confident without much evidence.
- `Verify` runs the project's checks and has a reviewer model confirm the work \
actually matches the request.
- `Orchestrate` splits a large piece of work into parallel assignments, runs \
them, reviews the result adversarially and repairs what the review confirms.

Delegating is not a way to avoid thinking — you still own the result and must \
check what comes back.

# Sizing up a request

Match the ceremony to the work. A question, a one-line fix or a lookup gets a
direct answer — no todo list, no plan, no clarifying questions. A normal task
gets done, with a todo list if it has several steps.

Only a genuinely large or irreversible piece of work warrants stopping first:
a new subsystem, a broad refactor, anything touching auth, money, migrations
or deletion, or a request loose enough that two competent people would build
different things. For those, use `AskUser` for the decisions you cannot make
yourself, then `PresentPlan`.

Ask a question only when the answer changes what you build. Check the code
first — an existing convention beats a question. For anything with an obvious
default, pick it, say you picked it, and continue.

# Communicating

- Answer in the user's language.
- Be concise. Skip preamble and flattery; lead with the answer or the action.
- Reference code as `path/to/file.py:42` so it is clickable.
- Explain what changed and why, not how the tools work. No summary tables of \
your own tool calls.
"""

PERMISSION_NOTES = {
    "ask": (
        "Permission mode: **ask**. Writes and commands need the user's approval. "
        "If a call is declined, do not retry it — ask what to do instead."
    ),
    "auto": (
        "Permission mode: **auto**. Edits inside the workspace run without "
        "prompting; risky commands still ask. Be correspondingly careful."
    ),
    "yolo": (
        "Permission mode: **yolo**. Nothing prompts. You are solely responsible "
        "for not destroying the user's work — re-read before overwriting, and "
        "never run a destructive command speculatively."
    ),
}

PLAN_MODE_NOTE = """\
**Plan mode is active.** Every write is blocked by the harness — not by
convention, by the permission engine. Attempting an edit wastes a turn.

Investigate with Read, Glob, Grep and read-only shell commands until you can
name the actual files that need to change, then call `PresentPlan`. If the
request is ambiguous in a way that changes the plan, use `AskUser` first —
and write those questions and options in the **user's language** (same as
their messages), not English by default.

A plan is worth reading only if it is specific. Name real paths. Say what
changes in each. Say what you are deliberately not doing. Write the plan
itself in the user's language too.
"""

EXPLORE_MODE_NOTE = """\
**Explore mode is active.** This is read-only investigation. The harness
blocks every write, mutating shell command, and planning ceremony tool.

Use Read, Glob, Grep and inspection-only shell commands. Report findings
clearly. Do not call PresentPlan, TodoWrite, or AskUser — exit explore mode
first if the user wants edits or a formal plan.
"""

PROJECT_INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md", ".aiharness/INSTRUCTIONS.md")

SUBAGENT_PROMPT = """\
You are a subagent working on one scoped task for a lead agent. You cannot see \
the parent conversation and cannot ask questions — work from the task \
description alone, and state any assumption you had to make.

Investigate with the tools you have, then return a single self-contained \
report. Include concrete specifics: exact file paths with line numbers, the \
code or commands that matter, and what you verified versus what you inferred. \
If you could not determine something, say so rather than guessing.

Do not describe your process or narrate which tools you used. Report findings.
"""


#: Cap nested instruction files (root + subdirs).
_MAX_INSTRUCTION_FILES = 8
#: Cap total instruction characters injected into the system prompt.
_MAX_INSTRUCTION_CHARS = 14_000


def read_project_instructions(workspace: Path) -> str:
    """Return repository-supplied agent instructions, if any.

    Loads root ``AGENTS.md`` / ``CLAUDE.md`` first, then nested copies under
    the workspace (Claude Code-style), skipping ignored directories.
    """
    from ..workspace.ignore import IgnoreMatcher

    root = workspace.resolve()
    matcher = IgnoreMatcher.for_workspace(root)
    blocks: list[str] = []
    total = 0

    def _add(path: Path, label: str) -> None:
        nonlocal total
        if len(blocks) >= _MAX_INSTRUCTION_FILES or total >= _MAX_INSTRUCTION_CHARS:
            return
        try:
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return
        if not text:
            return
        room = _MAX_INSTRUCTION_CHARS - total
        if len(text) > room:
            text = text[: room - 1] + "…"
        blocks.append(
            f"## {label}\n\n"
            f"These come from the repository and take precedence over your "
            f"general habits.\n\n{text}"
        )
        total += len(text)

    for name in PROJECT_INSTRUCTION_FILES:
        path = root / name
        if path.is_file():
            _add(path, name)

    # Nested instruction files (skip roots already loaded).
    seen = {str((root / name).resolve()) for name in PROJECT_INSTRUCTION_FILES}
    for name in ("AGENTS.md", "CLAUDE.md"):
        for path in root.rglob(name):
            if len(blocks) >= _MAX_INSTRUCTION_FILES:
                break
            try:
                key = str(path.resolve())
            except OSError:
                continue
            if key in seen or matcher.is_ignored(path):
                continue
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                continue
            seen.add(key)
            _add(path, rel)

    if not blocks:
        return ""
    return "# Project instructions\n\n" + "\n\n".join(blocks)


# Backwards-compatible alias used elsewhere in the package.
project_instructions = read_project_instructions


def build_system_prompt(
    workspace: Path,
    *,
    shell: str = "",
    skills_section: str = "",
    rules_section: str = "",
    extra: str = "",
    permission_mode: str = "ask",
    plan_mode: bool = False,
    explore_mode: bool = False,
) -> str:
    """Build the cache-stable system prompt.

    Nothing here may vary between turns of a session, or prompt caching will
    miss on every request. Volatile facts belong in
    :func:`build_environment_note` instead.

    Args:
      workspace: The agent's working directory.
      shell: Shell dialect name, ``posix`` or ``cmd``.
      skills_section: Rendered listing of available skills.
      rules_section: Global/project rules block from :mod:`aiharness.rules`.
      extra: Additional instructions appended verbatim.
      permission_mode: Active permission mode, described to the model.

    Returns:
      The complete system prompt.
    """
    from ..rules import load_rules

    sections = [
        BASE_PROMPT,
        (
            "# Environment\n\n"
            f"Working directory: {workspace}\n"
            f"Platform: {platform.system()} {platform.release()}\n"
            f"Shell: {shell or 'sh'}\n"
        ),
    ]

    if explore_mode:
        sections.append(EXPLORE_MODE_NOTE)
    elif plan_mode:
        sections.append(PLAN_MODE_NOTE)
    else:
        note = PERMISSION_NOTES.get(permission_mode, "")
        if note:
            sections.append(note)

    project = read_project_instructions(workspace)
    if project:
        sections.append(project)
    rules = rules_section or load_rules(workspace)[0]
    if rules:
        sections.append(rules)
    if skills_section:
        sections.append(skills_section)
    if extra:
        sections.append(extra)

    return "\n\n".join(
        section.strip() for section in sections if section and section.strip()
    )


def _git_summary(workspace: Path) -> str:
    """Return a short git status block (branch, dirty paths, recent commits)."""
    if not shutil.which("git") or not (workspace / ".git").exists():
        return ""
    try:
        hidden = hidden_subprocess_kwargs()
        # Windows + CREATE_NO_WINDOW can occasionally yield stdout=None; never
        # call .strip() on that — it aborted the whole turn with AttributeError.
        branch = (
            subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=GIT_COMMAND_TIMEOUT,
                check=False,
                **hidden,
            ).stdout
            or ""
        ).strip()
        status = (
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=GIT_COMMAND_TIMEOUT,
                check=False,
                **hidden,
            ).stdout
            or ""
        ).strip()
        log = (
            subprocess.run(
                ["git", "log", "-5", "--oneline", "--no-decorate"],
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=GIT_COMMAND_TIMEOUT,
                check=False,
                **hidden,
            ).stdout
            or ""
        ).strip()
    except (OSError, subprocess.SubprocessError, AttributeError):
        return ""

    if not branch:
        return ""
    lines = [f"git branch {branch}"]
    dirty_lines = [line for line in status.splitlines() if line.strip()]
    if dirty_lines:
        lines.append(f"{len(dirty_lines)} uncommitted change(s):")
        for line in dirty_lines[:12]:
            lines.append(f"  {line[:160]}")
        if len(dirty_lines) > 12:
            lines.append(f"  … {len(dirty_lines) - 12} more")
    else:
        lines.append("working tree clean")
    if log:
        lines.append("recent commits:")
        for line in log.splitlines()[:5]:
            lines.append(f"  {line[:120]}")
    return "\n".join(lines)


def build_environment_note(workspace: Path, query: str = "") -> str:
    """Build the volatile environment block attached to the newest user turn.

    Kept out of the system prompt on purpose: the date and git state change
    between turns, and putting them in the cached prefix would invalidate the
    cache on every single request.

    Args:
      workspace: The agent's working directory.
      query: Latest user text — used to rank an Aider-style repo map.

    Returns:
      A short markdown block, always non-empty.
    """
    from ..workspace.repomap import build_repo_map

    lines = [f"Date: {date.today().isoformat()}"]
    git = _git_summary(workspace)
    if git:
        lines.append(git)
    repo_map = build_repo_map(workspace, query)
    if repo_map:
        lines.append(repo_map)
    return "<environment>\n" + "\n".join(lines) + "\n</environment>"
