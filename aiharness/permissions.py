"""Permission engine.

Three modes, chosen at runtime with ``/mode``:

* ``ask``  — anything that mutates state or runs a command needs approval,
             unless an allow rule covers it.
* ``auto`` — reads and writes inside the workspace proceed silently;
             commands are screened and dangerous ones still prompt.
* ``yolo`` — nothing prompts, except the catastrophic set when
             ``block_catastrophic`` is on (recommended).

Rule syntax mirrors Claude Code: ``Tool(pattern)``.
    Read(*)                 every Read call
    Bash(git status)        exactly that command
    Bash(git diff:*)        any command starting with "git diff"
    Bash(npm run *)         glob match
    Write(src/**)           path glob, relative to the workspace
    mcp__github__*          every tool from one MCP server
"""

from __future__ import annotations

import fnmatch
import os
import re
import shlex
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .config.schema import PermissionConfig, PermissionMode


class Decision(Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass
class Verdict:
    decision: Decision
    reason: str = ""
    # Rule the user could add to stop being asked about this again.
    suggested_rule: str | None = None

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW


# --------------------------------------------------------------------------
# danger classification
# --------------------------------------------------------------------------

# Refused in every mode when block_catastrophic is on. These destroy data
# outside the workspace or the machine's ability to boot.
CATASTROPHIC = [
    (r"\brm\s+(-[a-zA-Z]*\s+)*-?[rRf]{1,2}[a-zA-Z]*\s+(/|~|/\*|\$HOME)(\s|$)", "recursive delete of / or $HOME"),
    (r"\bmkfs(\.\w+)?\b", "filesystem format"),
    (r"\bdd\b[^|]*\bof=/dev/(sd|nvme|hd|disk)", "raw write to a block device"),
    (r">\s*/dev/(sd|nvme|hd|disk)\w*", "raw write to a block device"),
    (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "fork bomb"),
    (r"\bchmod\s+(-R\s+)?[0-7]*777\s+/(\s|$)", "world-writable root"),
    (r"\bchown\s+-R\s+\S+\s+/(\s|$)", "recursive chown of /"),
    (r"\bshutdown\b|\breboot\b|\bhalt\b|\bpoweroff\b", "host power control"),
    (r"\bdiskpart\b|\bformat\s+[a-zA-Z]:", "Windows disk format"),
    (r"Remove-Item\s+.*-Recurse.*\s+[A-Za-z]:\\?(\s|$)", "recursive delete of a drive root"),
    (r"\bgit\s+push\s+.*--force.*\b(main|master)\b", "force-push to the default branch"),
]

# Prompt even in auto mode: irreversible, outward-facing, or credential-touching.
SENSITIVE = [
    (r"\brm\s+-[a-zA-Z]*r", "recursive delete"),
    (r"\bgit\s+(push|reset\s+--hard|clean\s+-[a-zA-Z]*f)", "irreversible git operation"),
    (r"\b(curl|wget|iwr|Invoke-WebRequest)\b", "network fetch"),
    (r"\b(sudo|runas|doas)\b", "privilege escalation"),
    (r"\b(npm|pnpm|yarn|pip|uv|cargo|gem)\s+(publish|upload)", "package publish"),
    (r"\b(docker|kubectl|helm|terraform|aws|gcloud|az)\b", "infrastructure command"),
    (r"\b(systemctl|service|sc\.exe|net\s+stop)\b", "service control"),
    (r"\bssh\b|\bscp\b|\brsync\b.*::", "remote host access"),
    (r">\s*/etc/|\bregedit\b|\breg\s+add\b", "system configuration write"),
    (r"\bhistory\b|\.ssh/|\.aws/credentials|\.env\b", "credential or secret path"),
]

# Tools that only observe. Free in every mode.
READ_ONLY_TOOLS = {"Read", "Glob", "Grep", "TodoRead", "Skill", "ListSkills", "WebFetch"}
# Tools that change the workspace.
MUTATING_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
# Tools usable while drafting a plan: investigation, plus the two that talk to
# the user. Everything else waits for approval.
PLAN_MODE_TOOLS = READ_ONLY_TOOLS | {
    "TodoWrite", "AskUser", "PresentPlan", "Research", "Screenshot",
    # Looking at a page is investigation, and investigation is the entire
    # purpose of plan mode. Blocking it produced plans written from memory:
    # the agent said "I cannot reach the network" and then estimated. Only
    # the read-only half is here — clicking and filling forms act on someone
    # else's system and still wait for approval.
    "BrowserNavigate", "BrowserSnapshot", "BrowserScreenshot",
}

# Commands that only report state. Anything not on this list is assumed to
# change something, which is the safe way round for a whitelist.
INSPECTION_COMMANDS = frozenset(
    {
        "ls", "pwd", "cat", "head", "tail", "wc", "file", "stat", "du", "df",
        "find", "grep", "rg", "fd", "tree", "which", "type", "echo", "date",
        "env", "printenv", "uname", "whoami", "ps", "top",
        "python", "python3", "node", "go", "cargo", "npm", "pnpm", "yarn", "uv",
    }
)
#: Subcommands of git that only read.
GIT_READ_SUBCOMMANDS = frozenset(
    {"status", "log", "diff", "show", "branch", "remote", "blame", "ls-files", "describe"}
)
#: Version/help flags make otherwise-writing commands harmless.
INSPECTION_FLAGS = ("--version", "-V", "--help", "-h", "list", "ls", "outdated")


def _is_inspection_command(command: str) -> bool:
    """Whether a single command only reads state."""
    tokens = command.strip().split()
    if not tokens:
        return True
    head = Path(tokens[0]).name.lower()

    if head == "git":
        return len(tokens) > 1 and tokens[1] in GIT_READ_SUBCOMMANDS
    if head not in INSPECTION_COMMANDS:
        return False
    # A package manager or interpreter is only safe in its reporting form.
    if head in ("npm", "pnpm", "yarn", "uv", "cargo", "go", "python", "python3", "node"):
        return any(flag in tokens[1:] for flag in INSPECTION_FLAGS)
    return True


def classify_command(command: str) -> tuple[str, str]:
    """Return ``(level, reason)`` where level is safe/sensitive/catastrophic."""
    normalised = " ".join(command.split())
    for pattern, reason in CATASTROPHIC:
        if re.search(pattern, normalised, re.IGNORECASE):
            return "catastrophic", reason
    for pattern, reason in SENSITIVE:
        if re.search(pattern, normalised, re.IGNORECASE):
            return "sensitive", reason
    return "safe", ""


def split_command(command: str) -> list[str]:
    """Split a shell line on &&, ||, ;, and | so each part is screened."""
    parts = re.split(r"\s*(?:&&|\|\||;|\|)\s*", command)
    return [p.strip() for p in parts if p.strip()]


_PATH_SKIP_EXACT = frozenset(
    {".", "..", "*", "?", "/dev/null", "dev/null", "nul", "NUL", "con", "CON"}
)


def extract_command_paths(command: str) -> list[str]:
    """Best-effort path tokens from a shell command for workspace checks."""
    found: list[str] = []
    seen: set[str] = set()
    for part in split_command(command):
        try:
            tokens = shlex.split(part, posix=os.name != "nt")
        except ValueError:
            tokens = part.split()
        for token in tokens:
            cleaned = token.strip().strip("'\"")
            if not cleaned or cleaned in _PATH_SKIP_EXACT:
                continue
            if cleaned.startswith("-"):
                continue
            if cleaned.startswith(("http://", "https://", "git@", "ssh://")):
                continue
            looks_path = (
                "/" in cleaned
                or "\\" in cleaned
                or cleaned.startswith((".", "~"))
                or bool(re.match(r"^[A-Za-z]:[\\/]", cleaned))
                or bool(Path(cleaned).suffix)
            )
            if looks_path and cleaned not in seen:
                seen.add(cleaned)
                found.append(cleaned)
    return found

# Tools allowed in Explore mode: observation only (stricter than plan).
EXPLORE_MODE_TOOLS = READ_ONLY_TOOLS


# --------------------------------------------------------------------------
# rule matching
# --------------------------------------------------------------------------

# The tool part accepts glob characters, so a whole family of tools can be
# addressed at once — `mcp__github__*` covers every tool from one MCP server.
RULE_RE = re.compile(
    r"^(?P<tool>[A-Za-z_][A-Za-z0-9_*?\[\]!-]*)\s*(?:\((?P<pattern>.*)\))?$", re.DOTALL
)


@dataclass
class Rule:
    tool: str  # an exact name, or a glob such as "mcp__github__*"
    pattern: str | None  # None means "every call to this tool"

    @classmethod
    def parse(cls, text: str) -> Rule | None:
        m = RULE_RE.match(text.strip())
        if not m:
            return None
        return cls(tool=m.group("tool"), pattern=m.group("pattern"))

    def matches_tool(self, tool: str) -> bool:
        if any(ch in self.tool for ch in "*?["):
            return fnmatch.fnmatch(tool, self.tool)
        return self.tool == tool

    def matches(self, tool: str, target: str) -> bool:
        if not self.matches_tool(tool):
            return False
        if self.pattern in (None, "", "*"):
            return True
        pattern = self.pattern
        # "git diff:*" -> prefix match, the form Claude Code uses.
        if pattern.endswith(":*"):
            return target.strip().startswith(pattern[:-2].strip())
        if any(ch in pattern for ch in "*?["):
            return fnmatch.fnmatch(target, pattern)
        return target.strip() == pattern.strip()


def rule_target(tool: str, args: dict[str, Any]) -> str:
    """The string a rule pattern is matched against, per tool."""
    if tool == "Bash":
        return str(args.get("command", ""))
    for key in ("file_path", "path", "pattern", "url", "name"):
        if key in args and args[key] is not None:
            return str(args[key])
    return ""


def suggest_rule(tool: str, args: dict[str, Any]) -> str:
    target = rule_target(tool, args)
    if tool == "Bash" and target:
        try:
            head = shlex.split(target)[:2]
        except ValueError:
            head = target.split()[:2]
        if head:
            return f"Bash({' '.join(head)}:*)"
    if tool in MUTATING_TOOLS and target:
        return f"{tool}({Path(target).suffix and '*' + Path(target).suffix or '*'})"
    return f"{tool}(*)"


# --------------------------------------------------------------------------
# engine
# --------------------------------------------------------------------------


class PermissionEngine:
    def __init__(self, cfg: PermissionConfig, workspace: Path):
        self.cfg = cfg
        self.workspace = workspace.resolve()
        self._allow = [r for r in (Rule.parse(x) for x in cfg.allow) if r]
        self._deny = [r for r in (Rule.parse(x) for x in cfg.deny) if r]
        self._ask = [r for r in (Rule.parse(x) for x in cfg.ask) if r]
        # Approvals granted with "allow for this session".
        self._session_allow: list[Rule] = []
        # Plan mode: investigate freely, change nothing. Set by the UI, and
        # enforced here rather than by asking the model nicely — a model that
        # decides the plan is obviously right will start editing otherwise.
        self.plan_mode = False
        # Explore mode: read-only investigation, no PresentPlan / todos / writes.
        self.explore_mode = False

    # -- runtime mutation -------------------------------------------------

    @property
    def mode(self) -> PermissionMode:
        return self.cfg.mode

    def set_mode(self, mode: PermissionMode) -> None:
        self.cfg.mode = mode

    def allow_for_session(self, rule_text: str) -> bool:
        rule = Rule.parse(rule_text)
        if not rule:
            return False
        self._session_allow.append(rule)
        return True

    def allow_persistently(self, rule_text: str) -> bool:
        """Session allow plus append to config ``allow`` (caller may save)."""
        if not self.allow_for_session(rule_text):
            return False
        if rule_text not in self.cfg.allow:
            self.cfg.allow.append(rule_text)
            parsed = Rule.parse(rule_text)
            if parsed:
                self._allow.append(parsed)
        return True

    def session_rules(self) -> list[str]:
        return [f"{r.tool}({r.pattern or '*'})" for r in self._session_allow]

    def set_plan_mode(self, active: bool) -> None:
        self.plan_mode = active
        if active:
            self.explore_mode = False

    def set_explore_mode(self, active: bool) -> None:
        self.explore_mode = active
        if active:
            self.plan_mode = False

    def _read_only_in_plan_mode(self, tool: str, target: str) -> bool:
        """Whether a call is safe to run while a plan is being drafted."""
        if tool in PLAN_MODE_TOOLS:
            return True
        if tool.startswith("mcp__"):
            return False  # a remote tool's effects are not ours to judge
        if tool != "Bash":
            return False
        # Bash is allowed only for commands that clearly just look at things.
        return all(_is_inspection_command(part) for part in split_command(target))

    def _allowed_in_explore_mode(self, tool: str, target: str) -> bool:
        """Stricter than plan: observation tools + inspection Bash only."""
        if tool in EXPLORE_MODE_TOOLS:
            return True
        if tool.startswith("mcp__"):
            return False
        if tool != "Bash":
            return False
        return all(_is_inspection_command(part) for part in split_command(target))

    # -- path boundary ----------------------------------------------------

    def allowed_roots(self) -> list[Path]:
        roots = [self.workspace]
        for extra in self.cfg.additional_directories:
            try:
                roots.append(Path(extra).expanduser().resolve())
            except OSError:
                continue
        return roots

    def path_in_scope(self, path: str | Path) -> bool:
        try:
            target = Path(path).expanduser().resolve()
        except (OSError, RuntimeError):
            return False
        for root in self.allowed_roots():
            try:
                target.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    # -- the decision -----------------------------------------------------

    def check(self, tool: str, args: dict[str, Any]) -> Verdict:
        target = rule_target(tool, args)

        # 0. explore / plan modes outrank everything, including yolo.
        if self.explore_mode and not self._allowed_in_explore_mode(tool, target):
            return Verdict(
                Decision.DENY,
                "explore mode: read-only investigation only. "
                "Exit explore mode before editing or running mutating commands.",
            )
        if self.plan_mode and not self._read_only_in_plan_mode(tool, target):
            return Verdict(
                Decision.DENY,
                "plan mode: nothing is written until the plan is approved. "
                "Finish investigating and call PresentPlan.",
            )

        # 1. explicit deny always wins
        for rule in self._deny:
            if rule.matches(tool, target):
                return Verdict(Decision.DENY, f"blocked by deny rule {rule.tool}({rule.pattern})")

        # 2. catastrophic screening, ahead of every allowance.
        # The whole line is screened as well as each part: some patterns (a
        # fork bomb, for one) span the separators that split_command breaks on.
        if tool == "Bash" and self.cfg.block_catastrophic:
            for part in [target, *split_command(target)]:
                level, reason = classify_command(part)
                if level == "catastrophic":
                    return Verdict(
                        Decision.DENY,
                        f"refused: {reason}. Run it yourself if you really mean it.",
                    )

        # 3. workspace boundary for file writes and Bash path tokens
        if tool in MUTATING_TOOLS and target:
            if not self.path_in_scope(target):
                if self.mode == "yolo":
                    pass  # yolo accepts out-of-tree writes
                else:
                    return Verdict(
                        Decision.ASK,
                        f"{target} is outside the workspace ({self.workspace})",
                        suggested_rule=None,
                    )
        if tool == "Bash" and target and self.mode != "yolo":
            for raw_path in extract_command_paths(target):
                if self.path_in_scope(raw_path):
                    continue
                # Relative bare names resolve inside the workspace — only
                # flag paths that clearly point elsewhere.
                try:
                    resolved = Path(raw_path).expanduser()
                except (OSError, RuntimeError):
                    continue
                if not resolved.is_absolute() and not raw_path.startswith(("~", "/", "\\")):
                    if not re.match(r"^[A-Za-z]:[\\/]", raw_path):
                        continue
                return Verdict(
                    Decision.ASK,
                    f"bash path {raw_path} is outside the workspace ({self.workspace})",
                    suggested_rule=suggest_rule(tool, args),
                )

        # 4. explicit allow rules (config + session)
        for rule in self._allow + self._session_allow:
            if rule.matches(tool, target):
                return Verdict(Decision.ALLOW, f"allowed by rule {rule.tool}({rule.pattern or '*'})")

        # 5. explicit ask rules — force a prompt even in auto/yolo
        for rule in self._ask:
            if rule.matches(tool, target):
                return Verdict(
                    Decision.ASK,
                    f"ask rule {rule.tool}({rule.pattern or '*'})",
                    suggested_rule=suggest_rule(tool, args),
                )

        # 6. mode-driven default
        if self.mode == "yolo":
            return Verdict(Decision.ALLOW, "yolo mode")

        if tool in READ_ONLY_TOOLS:
            return Verdict(Decision.ALLOW, "read-only tool")

        if self.mode == "auto":
            if tool in MUTATING_TOOLS:
                return Verdict(Decision.ALLOW, "auto mode: workspace write")
            if tool == "Bash":
                worst, reason = "safe", ""
                for part in split_command(target):
                    level, why = classify_command(part)
                    if level == "sensitive":
                        worst, reason = level, why
                if worst == "safe":
                    return Verdict(Decision.ALLOW, "auto mode: command looks safe")
                return Verdict(
                    Decision.ASK, f"auto mode: {reason}", suggested_rule=suggest_rule(tool, args)
                )
            return Verdict(Decision.ALLOW, "auto mode")

        # ask mode
        reason = "approval required"
        if tool == "Bash":
            for part in split_command(target):
                level, why = classify_command(part)
                if level == "sensitive":
                    reason = why
                    break
        return Verdict(Decision.ASK, reason, suggested_rule=suggest_rule(tool, args))
