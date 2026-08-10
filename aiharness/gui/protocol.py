"""Wire format between the Python backend and the web frontend.

One place defines every message shape, because the alternative is a browser
console full of ``undefined`` and no way to tell which side is wrong.

Two directions:

* **outbound** — the backend pushes events over a WebSocket as the agent
  streams: text deltas, tool activity, notices, compaction, status changes.
* **inbound** — the frontend asks for things: send a prompt, interrupt,
  switch model, answer a permission prompt.

Every message is ``{"type": ..., ...}`` and nothing else. Keeping the
envelope that boring means the JavaScript dispatcher is a single switch.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

#: Bumped when a message shape changes incompatibly. The frontend refuses to
#: run against a version it does not know, which turns a subtle rendering bug
#: into an obvious error message.
PROTOCOL_VERSION = 1


class Outbound(str, Enum):
    """Events the backend pushes to the frontend."""

    READY = "ready"  # handshake, sent once on connect
    TEXT = "text"  # a chunk of the model's visible answer
    THINKING = "thinking"  # a chunk of the reasoning stream
    TURN_START = "turn_start"  # a user turn began
    ACTIVITY = "activity"  # live "who is doing what" status line
    TURN_END = "turn_end"  # one model call finished, with usage
    DONE = "done"  # the whole turn finished
    TOOL_START = "tool_start"
    TOOL_END = "tool_end"
    NOTICE = "notice"  # info / warn / error line
    COMPACTED = "compacted"  # context was condensed
    STATUS = "status"  # model, mode, context, cost
    CONTEXT = "context"  # the full context breakdown
    SESSIONS = "sessions"  # the session list changed
    SKILLS = "skills"  # installed skills and where they came from
    WORKSPACE = "workspace"  # the project directory changed
    CONFIG = "config"  # accounts / models / roles changed
    TODOS = "todos"
    PLAN = "plan"
    ASK = "ask"  # clarifying questions, expects an answer
    PERMISSION = "permission"  # tool approval, expects an answer
    HEARTBEAT = "heartbeat"
    LEARN_RESULT = "learn_result"
    MARKET_RESULT = "market_result"
    MARKET_ALERT = "market_alert"
    SEARCH_HITS = "search_hits"
    CANVAS_HINT = "canvas_hint"
    ONBOARDING = "onboarding"
    EDIT_REVIEW = "edit_review"  # pending Write/Edit Apply/Reject queue
    PATH_INDEX = "path_index"
    FILE_TREE = "file_tree"
    FILE_PREVIEW = "file_preview"
    RULES_LIST = "rules_list"
    MEMORIES = "memories"
    QUEST = "quest"
    SCREENSHOT = "screenshot"  # composer capture result (base64 image)
    ERROR = "error"
    # Codex panel (sibling runtime via codex app-server)
    CODEX_STATUS = "codex_status"
    CODEX_TEXT = "codex_text"
    CODEX_NOTICE = "codex_notice"
    CODEX_PERMISSION = "codex_permission"
    CODEX_DONE = "codex_done"
    CODEX_ERROR = "codex_error"
    CODEX_ACTIVITY = "codex_activity"
    CODEX_TOOL_START = "codex_tool_start"
    CODEX_TOOL_END = "codex_tool_end"
    CODEX_THINKING = "codex_thinking"
    # Claude Code panel
    CLAUDE_STATUS = "claude_status"
    CLAUDE_TEXT = "claude_text"
    CLAUDE_NOTICE = "claude_notice"
    CLAUDE_PERMISSION = "claude_permission"
    CLAUDE_DONE = "claude_done"
    CLAUDE_ERROR = "claude_error"
    CLAUDE_ACTIVITY = "claude_activity"
    CLAUDE_TOOL_START = "claude_tool_start"
    CLAUDE_TOOL_END = "claude_tool_end"
    CLAUDE_THINKING = "claude_thinking"


class Inbound(str, Enum):
    """Commands the frontend sends to the backend."""

    PROMPT = "prompt"
    STEER = "steer"  # mid-turn guidance into the live agent
    INTERRUPT = "interrupt"
    ANSWER = "answer"  # reply to an ASK
    APPROVE = "approve"  # reply to a PERMISSION
    PLAN_DECISION = "plan_decision"
    NEW_SESSION = "new_session"
    OPEN_SESSION = "open_session"
    DELETE_SESSION = "delete_session"
    ARCHIVE_SESSION = "archive_session"
    TOGGLE_ARCHIVED = "toggle_archived"
    SET_WORKSPACE = "set_workspace"
    PICK_WORKSPACE = "pick_workspace"
    FORGET_WORKSPACE = "forget_workspace"
    ADD_SKILL_PATH = "add_skill_path"
    REMOVE_SKILL_PATH = "remove_skill_path"
    LIST_SKILLS = "list_skills"  # show what is loaded, without rescanning
    RELOAD_SKILLS = "reload_skills"
    SET_CONTEXT = "set_context"
    SET_AUTO_COMPACT = "set_auto_compact"
    SET_AUTO_CLASSIFY = "set_auto_classify"
    SET_AUTO_APPLY_EDITS = "set_auto_apply_edits"
    SET_PLAN_MODE = "set_plan_mode"
    SET_EXPLORE_MODE = "set_explore_mode"
    SET_DRAFT = "set_draft"
    OPEN_PATH = "open_path"
    LEARN_SKILLS = "learn_skills"
    MARKET_QUOTE = "market_quote"
    MARKET_HISTORY = "market_history"
    MARKET_BACKTEST = "market_backtest"
    MARKET_ALERT_ADD = "market_alert_add"
    MARKET_ALERT_LIST = "market_alert_list"
    MARKET_ALERT_DELETE = "market_alert_delete"
    PAPER_STATUS = "paper_status"
    SEARCH_CONTENT = "search_content"
    CLEAR_SESSION = "clear_session"
    REWIND_TURN = "rewind_turn"
    SET_MODEL = "set_model"
    SET_MODE = "set_mode"
    SET_EFFORT = "set_effort"
    SET_THEME = "set_theme"
    SET_LANGUAGE = "set_language"
    ADD_ACCOUNT = "add_account"
    REMOVE_ACCOUNT = "remove_account"
    LIST_ACCOUNT_MODELS = "list_account_models"
    ADD_MODEL = "add_model"
    REMOVE_MODEL = "remove_model"
    SET_ROLE = "set_role"
    SET_CAPABILITY = "set_capability"  # opt in to desktop / browser control
    SET_ACCOUNT_PROXY = "set_account_proxy"
    SAVE_CONFIG = "save_config"
    REFRESH = "refresh"  # resend status, sessions and config
    LOAD_TRANSCRIPT = "load_transcript"  # older page for the open session
    COMPACT = "compact"
    UNCOMPACT = "uncompact"
    START_HEARTBEAT = "start_heartbeat"
    STOP_HEARTBEAT = "stop_heartbeat"
    EDIT_DECISION = "edit_decision"  # apply / reject / apply_all / reject_all
    LIST_PATHS = "list_paths"
    LIST_TREE = "list_tree"
    PREVIEW_PATH = "preview_path"
    LIST_RULES = "list_rules"
    SAVE_RULE = "save_rule"
    DELETE_RULE = "delete_rule"
    LIST_MEMORIES = "list_memories"
    ADD_MEMORY = "add_memory"
    UPDATE_MEMORY = "update_memory"
    DELETE_MEMORY = "delete_memory"
    LIST_QUEST = "list_quest"
    START_QUEST = "start_quest"
    QUEST_STEP = "quest_step"
    RESUME_QUEST = "resume_quest"
    CONTINUE_WORK = "continue_work"  # resume open todos / quest in one click
    CLEAR_QUEST = "clear_quest"
    NEW_WINDOW = "new_window"
    SAVE_CANVAS = "save_canvas"
    CAPTURE_SCREEN = "capture_screen"  # user screenshot → composer attach
    OPEN_URL = "open_url"  # open http(s) in the system browser
    SET_MODEL_VISION = "set_model_vision"  # auto | on | off
    # Codex panel
    CODEX_SET_HOME = "codex_set_home"
    CODEX_SET_PROFILE = "codex_set_profile"
    CODEX_UPSERT_PROFILE = "codex_upsert_profile"
    CODEX_DELETE_PROFILE = "codex_delete_profile"
    CODEX_IMPORT_ACCOUNT = "codex_import_account"
    CODEX_START = "codex_start"
    CODEX_STOP = "codex_stop"
    CODEX_PROMPT = "codex_prompt"
    CODEX_INTERRUPT = "codex_interrupt"
    CODEX_APPROVE = "codex_approve"
    CODEX_SET_MODEL = "codex_set_model"
    CODEX_SET_EFFORT = "codex_set_effort"
    CODEX_SET_MODE = "codex_set_mode"
    CODEX_NEW_SESSION = "codex_new_session"
    CODEX_OPEN_SESSION = "codex_open_session"
    CODEX_DELETE_SESSION = "codex_delete_session"
    CODEX_ARCHIVE_SESSION = "codex_archive_session"
    CODEX_SET_WORKSPACE = "codex_set_workspace"
    CODEX_FORGET_WORKSPACE = "codex_forget_workspace"
    CODEX_TOGGLE_ARCHIVED = "codex_toggle_archived"
    # Claude Code panel
    CLAUDE_SET_PROFILE = "claude_set_profile"
    CLAUDE_UPSERT_PROFILE = "claude_upsert_profile"
    CLAUDE_DELETE_PROFILE = "claude_delete_profile"
    CLAUDE_IMPORT_ACCOUNT = "claude_import_account"
    CLAUDE_START = "claude_start"
    CLAUDE_STOP = "claude_stop"
    CLAUDE_PROMPT = "claude_prompt"
    CLAUDE_INTERRUPT = "claude_interrupt"
    CLAUDE_APPROVE = "claude_approve"
    CLAUDE_SET_MODEL = "claude_set_model"
    CLAUDE_SET_EFFORT = "claude_set_effort"
    CLAUDE_SET_MODE = "claude_set_mode"
    CLAUDE_LOGIN = "claude_login"
    CLAUDE_NEW_SESSION = "claude_new_session"
    CLAUDE_OPEN_SESSION = "claude_open_session"
    CLAUDE_DELETE_SESSION = "claude_delete_session"
    CLAUDE_ARCHIVE_SESSION = "claude_archive_session"
    CLAUDE_SET_WORKSPACE = "claude_set_workspace"
    CLAUDE_FORGET_WORKSPACE = "claude_forget_workspace"
    CLAUDE_TOGGLE_ARCHIVED = "claude_toggle_archived"


def message(outbound: Outbound, **payload: Any) -> dict[str, Any]:
    """Build an outbound message.

    The first argument must not be named ``kind`` — several payloads
    (canvas hints, tool displays) use that key.
    """
    return {"type": outbound.value, **payload}


# --------------------------------------------------------------------------
# payload shapes
# --------------------------------------------------------------------------


@dataclass
class StatusPayload:
    """Everything the status bar shows, in one message."""

    model: str
    account: str
    effort: str
    mode: str
    context_used: int
    context_window: int
    cache_hit: float
    cost: float
    busy: bool
    plan_mode: bool
    session_id: str
    session_title: str
    explore_mode: bool = False
    #: Absolute path of the working directory, and its last component.
    workspace: str = ""
    workspace_name: str = ""
    auto_compact: bool = True
    #: When True, each prompt is scored and large work enters plan mode.
    auto_classify: bool = True
    #: When True and mode is yolo, Write/Edit skip the review queue.
    auto_apply_edits: bool = False
    #: UI language preference: ``auto`` or a code from :mod:`aiharness.gui.locale`.
    language: str = "auto"
    #: Unsent text in the composer for this session.
    draft: str = ""
    #: @ paths attached on the latest user turn (context panel).
    turn_refs: list[str] = field(default_factory=list)
    compact_threshold: float = 0.82
    context_options: list[int] = field(default_factory=list)
    heartbeat: bool = False
    #: Limits accepted, waiting for the composer to supply the goal.
    heartbeat_armed: bool = False
    configured: bool = True
    #: Process-lifetime cache hit rate (resets when the app restarts).
    #: ``cache_hit`` keeps the same meaning for older frontends.
    run_cache_hit: float = 0.0
    #: Durable cache hit rate for the open session across restarts.
    session_cache_hit: float = 0.0
    #: Sticky activity line for the viewed session when it is busy (so a
    #: switch-back can restore "k3@Kimi 思考中…" instead of an empty dock).
    activity: str = ""
    #: True when the open chat has unfinished todos/quest and is idle.
    resume_available: bool = False
    #: Count of incomplete TodoWrite items (for the continue CTA label).
    open_todo_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ToolStartPayload:
    call_id: str
    name: str
    args: dict[str, Any]
    #: Short one-line rendering, so the frontend does not re-implement it.
    headline: str = ""


@dataclass
class ToolEndPayload:
    call_id: str
    name: str
    summary: str
    content: str
    is_error: bool
    duration: float
    display: dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionSummary:
    id: str
    title: str
    updated: str
    messages: int
    cost: float
    active: bool = False


@dataclass
class AccountSummary:
    id: str
    base_url: str
    key: str
    models: list[str] = field(default_factory=list)
    enabled: bool = True


@dataclass
class ModelSummary:
    id: str
    model: str
    accounts: list[str]
    context_windows: list[int]
    default_context: int
    effort_levels: list[str]


@dataclass
class ConfigPayload:
    """The whole configurable surface, for the settings panel."""

    accounts: list[dict[str, Any]] = field(default_factory=list)
    models: list[dict[str, Any]] = field(default_factory=list)
    roles: list[dict[str, Any]] = field(default_factory=list)
    #: Tools that reach outside the workspace and are therefore opt-in.
    capabilities: list[dict[str, Any]] = field(default_factory=list)
    #: Proxy presets offered as autocomplete in the account form.
    proxy_presets: list[str] = field(default_factory=list)
    #: Language picker entries ``[{code, label}, …]``.
    languages: list[dict[str, str]] = field(default_factory=list)
    #: Sponsorship links + whether the Alipay QR asset is present.
    support: dict[str, Any] = field(default_factory=dict)
    ready: bool = False
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProtocolError(Exception):
    """Raised when an inbound message cannot be understood."""


def parse_inbound(raw: Any) -> tuple[Inbound, dict[str, Any]]:
    """Validate an inbound message.

    Args:
      raw: The decoded JSON payload.

    Returns:
      The command and its arguments.

    Raises:
      ProtocolError: If the envelope is malformed or the command is unknown.
    """
    if not isinstance(raw, dict):
        raise ProtocolError("message must be an object")
    kind = raw.get("type")
    if not isinstance(kind, str):
        raise ProtocolError("message needs a string 'type'")
    try:
        command = Inbound(kind)
    except ValueError as error:
        raise ProtocolError(f"unknown command '{kind}'") from error
    return command, {k: v for k, v in raw.items() if k != "type"}
