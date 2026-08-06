"""Learning workflows from what the user actually does.

The premise: after a few weeks of use, the session log contains the user's
working habits in a form nobody has written down. The same setup command
before every test run. The same three files opened together. The same review
step the user asks for every single time and the agent forgets every single
time.

This module mines that log and proposes skills. Two constraints shape it:

* **It proposes, it does not install.** A skill silently written from
  inferred habits would change the agent's behaviour in ways the user never
  agreed to and cannot easily trace. Every candidate is shown and saved only
  on confirmation.
* **It needs repetition, not one good session.** A pattern seen once is a
  coincidence. The threshold is deliberately conservative, because a wrong
  skill is worse than no skill: it fires on the wrong tasks and has to be
  debugged through the model's behaviour.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..constants import (
    LEARNING_MIN_OCCURRENCES,
    LEARNING_MIN_SESSION_MESSAGES,
    LEARNING_SESSION_DIGEST_CHARS,
    LEARNING_SESSION_LIMIT,
)
from ..providers.base import Message
from ..session.store import SessionStore

if TYPE_CHECKING:  # pragma: no cover
    from ..providers.router import Router, Selection

#: Tool calls that say nothing about the user's habits.
UNINTERESTING_TOOLS = frozenset({"TodoWrite", "Inbox", "Team", "ListSkills"})
#: Characters of a user request kept in a digest.
REQUEST_CHARS = 300
#: Tool calls summarised per session.
MAX_TOOLS_PER_SESSION = 40


@dataclass
class SessionDigest:
    """A compressed view of one past session, for the miner to read."""

    session_id: str
    title: str
    requests: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)

    def render(self) -> str:
        parts = [f"## session {self.session_id} — {self.title or '(untitled)'}"]
        if self.requests:
            parts.append("asked:\n" + "\n".join(f"  - {r}" for r in self.requests))
        if self.tools:
            parts.append("tool sequence: " + " → ".join(self.tools))
        if self.commands:
            parts.append("commands:\n" + "\n".join(f"  $ {c}" for c in self.commands))
        if self.files:
            parts.append("files touched: " + ", ".join(self.files))
        text = "\n".join(parts)
        return text[:LEARNING_SESSION_DIGEST_CHARS]


@dataclass
class SkillCandidate:
    """A proposed skill, awaiting the user's approval."""

    name: str
    description: str
    body: str
    evidence: list[str] = field(default_factory=list)
    occurrences: int = 0

    def to_markdown(self) -> str:
        """Render as a SKILL.md file."""
        description = " ".join(self.description.split())
        return (
            "---\n"
            f"name: {self.name}\n"
            f"description: {description}\n"
            "---\n\n"
            f"{self.body.strip()}\n"
        )

    def summary(self) -> str:
        return (
            f"**{self.name}** (seen in {self.occurrences} session(s))\n"
            f"{' '.join(self.description.split())[:220]}"
        )


# --------------------------------------------------------------------------
# mining the log
# --------------------------------------------------------------------------


def digest_session(handle: Any) -> SessionDigest | None:
    """Compress one stored session into the facts worth mining.

    Returns:
      A digest, or ``None`` when the session is too short to be informative.
    """
    messages = handle.full_history
    if len(messages) < LEARNING_MIN_SESSION_MESSAGES:
        return None

    digest = SessionDigest(session_id=handle.meta.id, title=handle.meta.title)
    seen_files: set[str] = set()

    for message in messages:
        if message.role == "user" and not message.meta.get("compacted"):
            text = message.meta.get("user_text") or message.content
            cleaned = re.sub(r"<environment>.*?</environment>", "", text, flags=re.DOTALL)
            cleaned = " ".join(cleaned.split())
            if cleaned:
                digest.requests.append(cleaned[:REQUEST_CHARS])
        elif message.role == "assistant":
            for call in message.tool_calls:
                if call.name in UNINTERESTING_TOOLS:
                    continue
                if len(digest.tools) < MAX_TOOLS_PER_SESSION:
                    digest.tools.append(call.name)
                _collect_arguments(call, digest, seen_files)

    if not digest.requests:
        return None
    return digest


def _collect_arguments(call: Any, digest: SessionDigest, seen_files: set[str]) -> None:
    """Pull commands and file paths out of one tool call."""
    try:
        args = call.parsed()
    except ValueError:
        return
    if call.name == "Bash":
        command = " ".join(str(args.get("command", "")).split())
        if command and command not in digest.commands:
            digest.commands.append(command[:200])
    path = args.get("file_path")
    if path and path not in seen_files:
        seen_files.add(str(path))
        digest.files.append(str(path))


def collect_digests(
    store: SessionStore,
    workspace: Path | None = None,
    limit: int = LEARNING_SESSION_LIMIT,
) -> list[SessionDigest]:
    """Digest the most recent sessions."""
    digests: list[SessionDigest] = []
    for meta in store.list(workspace=workspace, limit=limit):
        handle = store.open(meta.id)
        if handle is None:
            continue
        digest = digest_session(handle)
        if digest is not None:
            digests.append(digest)
    return digests


def repeated_commands(digests: list[SessionDigest], minimum: int) -> dict[str, int]:
    """Commands that appear across at least ``minimum`` distinct sessions.

    Counting sessions rather than occurrences matters: a loop that ran
    ``pytest`` twelve times in one afternoon is one habit, not twelve.
    """
    counts: dict[str, int] = {}
    for digest in digests:
        for command in set(digest.commands):
            head = " ".join(command.split()[:3])
            counts[head] = counts.get(head, 0) + 1
    return {command: n for command, n in counts.items() if n >= minimum}


# --------------------------------------------------------------------------
# the miner
# --------------------------------------------------------------------------

MINER_PROMPT = """\
You are reading a user's past sessions with a coding agent, looking for
habits worth writing down as reusable skills.

A good candidate is something the user needed **repeatedly** and had to
re-explain, or something the agent kept getting wrong the same way. For
example: a project-specific build incantation, a review step they always ask
for, a file layout convention, a tool they always want used for a given job.

Reject:

- Anything you saw only once. One session is a coincidence.
- Anything already obvious from the codebase — a skill that says "this is a
  Python project" is noise.
- Restatements of the agent's general instructions.
- Anything that would fire on unrelated tasks. A skill with a vague trigger
  is worse than no skill: it derails work it has nothing to do with.

Reply with JSON only, no prose, no code fence:

{"candidates": [
  {"name": "kebab-case-slug",
   "description": "What it does AND exactly when to use it. This is the only
     text the model sees when deciding whether to load the skill, so the
     trigger conditions must be concrete and specific.",
   "body": "The full instructions, in markdown. Write what to do, in what
     order, with the real commands and paths. Be specific enough that
     following it produces the same result the user got before.",
   "evidence": ["session id or the observation that supports this"],
   "occurrences": <how many distinct sessions showed it>}
]}

Return an empty list if nothing clears the bar. That is a perfectly good
answer and much better than inventing something.
"""


async def mine_skills(
    digests: list[SessionDigest],
    router: Router,
    selection: Selection,
    *,
    minimum: int = LEARNING_MIN_OCCURRENCES,
) -> list[SkillCandidate]:
    """Ask a model to find repeated habits in the digests.

    Args:
      digests: Compressed sessions to read.
      router: Router used to reach the mining model.
      selection: Which model does the mining.
      minimum: Sessions a pattern must appear in to be proposed.

    Returns:
      Candidates that met the threshold, strongest first.
    """
    if len(digests) < minimum:
        return []

    hints = repeated_commands(digests, minimum)
    body = "\n\n".join(digest.render() for digest in digests)
    if hints:
        listed = "\n".join(f"- `{cmd}` in {n} sessions" for cmd, n in sorted(hints.items()))
        body += f"\n\n## Commands that recur across sessions\n{listed}"

    reply = await router.ask(
        selection,
        [
            Message(role="system", content=MINER_PROMPT),
            Message(
                role="user",
                content=(
                    f"{len(digests)} sessions. A pattern needs at least {minimum} "
                    f"distinct sessions to qualify.\n\n{body}"
                ),
            ),
        ],
        role="learner",
    )

    try:
        payload = _extract_json(reply.message.content)
    except ValueError:
        return []

    candidates: list[SkillCandidate] = []
    for item in payload.get("candidates") or []:
        candidate = _build_candidate(item, minimum)
        if candidate is not None:
            candidates.append(candidate)
    candidates.sort(key=lambda c: c.occurrences, reverse=True)
    return candidates


def _build_candidate(item: Any, minimum: int) -> SkillCandidate | None:
    """Validate one proposed candidate, dropping anything unusable."""
    if not isinstance(item, dict):
        return None
    name = _slugify(str(item.get("name") or ""))
    description = str(item.get("description") or "").strip()
    body = str(item.get("body") or "").strip()
    if not (name and description and body):
        return None
    try:
        occurrences = int(item.get("occurrences", 0))
    except (TypeError, ValueError):
        occurrences = 0
    if occurrences < minimum:
        return None
    return SkillCandidate(
        name=name,
        description=description,
        body=body,
        evidence=[str(e) for e in (item.get("evidence") or [])],
        occurrences=occurrences,
    )


def _slugify(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in name.strip().lower())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-")


def _extract_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", stripped, re.DOTALL)
    if fenced:
        stripped = fenced.group(1).strip()
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(stripped[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    raise ValueError("miner did not return JSON")


def save_candidate(candidate: SkillCandidate, skills_dir: Path) -> Path:
    """Write an approved candidate to disk as a real skill.

    Args:
      candidate: The approved candidate.
      skills_dir: Directory that holds skill folders.

    Returns:
      The path of the written ``SKILL.md``.

    Raises:
      FileExistsError: If a skill of that name already exists. Overwriting a
        skill the user may have edited by hand is not this function's call.
    """
    target = skills_dir / candidate.name
    path = target / "SKILL.md"
    if path.exists():
        raise FileExistsError(f"{path} already exists")
    target.mkdir(parents=True, exist_ok=True)
    path.write_text(candidate.to_markdown(), encoding="utf-8")
    return path
