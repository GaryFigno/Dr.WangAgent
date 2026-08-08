"""Central tuning constants.

Every threshold, limit and magic value used by the harness lives here so it
can be found, reviewed and adjusted in one place. Nothing in this module
imports from the rest of the package.
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------

#: The name shown to the user: window title, tray tooltip, sidebar, about.
APP_NAME: str = "Dr.Wang"
#: The internal identifier: Python package, CLI command, config directory,
#: tray icon id. Deliberately *not* the display name — changing it would move
#: ``%LOCALAPPDATA%\\aiharness`` and orphan every existing session and key.
APP_SLUG: str = "aiharness"

# --------------------------------------------------------------------------
# Token estimation
# --------------------------------------------------------------------------

#: Approximate characters per token for Latin-script text.
CHARS_PER_TOKEN_LATIN: float = 3.7
#: Approximate tokens per character for CJK text.
TOKENS_PER_CHAR_CJK: float = 1.05
#: Per-message overhead the chat format adds (role markers, separators).
TOKENS_PER_MESSAGE_OVERHEAD: int = 4
#: Per-tool-call overhead in the serialised request.
TOKENS_PER_TOOL_CALL_OVERHEAD: int = 8
#: Calibration samples kept when correcting the estimate against real usage.
CALIBRATION_SAMPLE_WINDOW: int = 8
#: Calibration ratios outside this band are treated as outliers and dropped.
CALIBRATION_RATIO_MIN: float = 0.4
CALIBRATION_RATIO_MAX: float = 3.0

# --------------------------------------------------------------------------
# Context compaction
# --------------------------------------------------------------------------

#: Minimum messages that must exist before compaction is worth attempting.
COMPACT_MIN_MESSAGES: int = 2
#: Floor for the summary token budget, regardless of window size.
COMPACT_SUMMARY_MIN_TOKENS: int = 1024
#: Characters of each tool result included in the text sent to the compactor.
COMPACT_TOOL_RESULT_CHARS: int = 2000
#: Characters of each chat message included in the text sent to the compactor.
COMPACT_MESSAGE_CHARS: int = 4000
#: Characters of each tool-call argument blob included for the compactor.
COMPACT_TOOL_ARGS_CHARS: int = 800
#: Fractions of an over-long tool result kept at the head and tail.
TRUNCATE_HEAD_FRACTION: float = 0.6
TRUNCATE_TAIL_FRACTION: float = 0.3
#: Keep roughly this many tokens of recent tool output when pruning history.
#: Tightened so long verify/Bash turns digest sooner than a full 40k hangover.
PRUNE_PROTECT_TOKENS: int = 20_000
#: Only rewrite older tool outputs when reclaiming at least this many tokens.
PRUNE_MINIMUM_TOKENS: int = 8_000
#: Do not prune tool results from the newest N user turns.
PRUNE_KEEP_USER_TURNS: int = 1
#: Tool names whose outputs stay intact during prune (skills etc.).
PRUNE_PROTECTED_TOOLS: frozenset[str] = frozenset({"Skill", "ListSkills"})
#: Default caps for per-tool wire digests (overridable via ContextConfig).
BASH_SUCCESS_RESULT_CHARS: int = 2_500
BASH_ERROR_RESULT_CHARS: int = 6_000
READ_RESULT_CHARS: int = 12_000
#: Max characters kept for a prune-time one-line digest body.
PRUNE_DIGEST_CHARS: int = 280
#: Identical tool+args repeats before the harness refuses further invokes.
DOOM_LOOP_THRESHOLD: int = 3
#: After a context-overflow model error, compact once and retry this many times.
OVERFLOW_COMPACT_RETRIES: int = 1
#: Inject a short todo/status reminder into the model every N agent turns.
REMINDER_EVERY_TURNS: int = 8

# --------------------------------------------------------------------------
# Filesystem tools
# --------------------------------------------------------------------------

#: Default maximum lines returned by a single Read call.
MAX_READ_LINES: int = 2000
#: Longest single line returned verbatim before it is clipped.
MAX_LINE_CHARS: int = 2000
#: Bytes sampled from the head of a file when sniffing for binary content.
BINARY_SNIFF_BYTES: int = 8192
#: Fraction of sampled bytes that must be printable for a file to count as text.
BINARY_PRINTABLE_THRESHOLD: float = 0.7
#: Default cap on Glob results.
GLOB_RESULT_LIMIT: int = 300
#: Multiplier applied to the result limit when deciding how far to walk.
GLOB_SCAN_MULTIPLIER: int = 4
#: Default cap on Grep result lines.
GREP_RESULT_LIMIT: int = 200
#: Seconds before an external ripgrep invocation is abandoned.
GREP_SUBPROCESS_TIMEOUT: float = 60.0
#: Hard ceiling for one PARALLEL_SAFE tool (Read/Glob/Grep/…) so a stuck
#: peer cannot hold ``asyncio.gather`` forever.
PARALLEL_TOOL_TIMEOUT: float = 90.0
#: Hard ceiling for any other single tool invoke (Bash has its own cap).
TOOL_INVOKE_TIMEOUT: float = 180.0
#: Max chars of Edit hunk text sent to the edit-review UI.
EDIT_REVIEW_PREVIEW_CHARS: int = 4000
#: Tighter cap for whole-file Write previews in the review UI.
WRITE_REVIEW_PREVIEW_CHARS: int = 1200
#: Characters of filename prefix used when suggesting a near-miss path.
PATH_SUGGEST_PREFIX_CHARS: int = 4
#: How many near-miss filenames to offer.
PATH_SUGGEST_LIMIT: int = 3
#: Files listed alongside a skill's SKILL.md.
SKILL_BUNDLED_FILE_LIMIT: int = 40
#: Longest skill body injected when a skill is invoked.
SKILL_MAX_BODY_CHARS: int = 60000
#: Longest skill description shown in the system prompt listing.
SKILL_MAX_DESCRIPTION_CHARS: int = 400
#: Entries shown when Read is pointed at a directory.
DIRECTORY_LISTING_LIMIT: int = 200

# --------------------------------------------------------------------------
# Shell
# --------------------------------------------------------------------------

#: Characters of combined output retained from a command.
SHELL_MAX_OUTPUT_CHARS: int = 30000
#: Default and hard-capped command timeouts, in seconds.
SHELL_DEFAULT_TIMEOUT: float = 120.0
SHELL_MAX_TIMEOUT: float = 600.0
#: Outer wait around Bash so the shell's own kill finishes first.
BASH_OUTER_TIMEOUT: float = SHELL_MAX_TIMEOUT + 10.0
#: Characters of a command echoed into the progress line.
SHELL_COMMAND_ECHO_CHARS: int = 200
#: Bytes sampled when guessing the encoding of command output.
UTF16_SNIFF_BYTES: int = 512
#: Share of control/replacement characters above which a decode is judged
#: wrong. A codepage decoder accepts almost any bytes, so "it did not raise"
#: is not evidence that the text is right.
GARBLING_LIMIT: float = 0.10
#: Below this codepoint everything is a control character. Their presence
#: in decoded output is the signature of the wrong codec.
FIRST_PRINTABLE_CODEPOINT: int = 32
#: Characters of a command used as its fallback display label.
SHELL_LABEL_CHARS: int = 60

# --------------------------------------------------------------------------
# Provider transport
# --------------------------------------------------------------------------

#: Default per-request timeout, in seconds.
REQUEST_TIMEOUT: float = 300.0
#: Connection pool sizing per API account.
HTTP_MAX_CONNECTIONS: int = 16
HTTP_MAX_KEEPALIVE: int = 8
#: Attempts against a single account before moving to the next one.
MAX_ATTEMPTS_PER_ACCOUNT: int = 2
#: Base and ceiling for exponential backoff between attempts, in seconds.
RETRY_BACKOFF_BASE: float = 0.5
RETRY_BACKOFF_CEILING: float = 8.0
#: Seconds an account is skipped after a rate-limit response.
RATE_LIMIT_COOLDOWN: float = 5.0
#: Seconds an account is skipped after an authentication failure.
AUTH_FAILURE_COOLDOWN: float = 300.0
#: Window used when enforcing a per-account requests-per-minute ceiling.
RPM_WINDOW_SECONDS: float = 60.0
#: Characters of a provider error body surfaced to the user.
ERROR_DETAIL_CHARS: int = 600
#: Errors retained when reporting that every account failed.
ROUTE_ERROR_HISTORY: int = 6
#: Seconds allowed for the credential probe used by `aih doctor`.
PROBE_TIMEOUT: float = 15.0

# --------------------------------------------------------------------------
# Agent loop
# --------------------------------------------------------------------------

#: Default ceiling on model turns in a single user request.
DEFAULT_MAX_AGENT_TURNS: int = 100
#: Default cap on automatic "continue open todos" chains per chat.
DEFAULT_MAX_AUTO_CONTINUES: int = 3
#: Default turn budget for a delegated subtask.
DELEGATE_MAX_TURNS: int = 15
#: Default turn budget for an explicitly spawned subagent.
TASK_MAX_TURNS: int = 20
#: Default turn budget for review and verification subagents.
REVIEW_MAX_TURNS: int = 14
#: How deep subagents may nest before further spawning is refused.
MAX_SUBAGENT_DEPTH: int = 3
#: Subagents allowed to run concurrently by default.
DEFAULT_PARALLEL_AGENTS: int = 3
#: Hard ceiling on adversarial passes in one Challenge call.
MAX_ADVERSARIAL_ROUNDS: int = 4
#: Characters of raw tool arguments shown when they fail to parse.
MALFORMED_ARGS_PREVIEW_CHARS: int = 400
#: Characters of check output embedded in a verification prompt.
VERIFY_CHECK_OUTPUT_CHARS: int = 6000
#: Seconds allowed for verification commands.
VERIFY_COMMAND_TIMEOUT: float = 300.0
#: Characters of a research angle used as a subagent label.
AGENT_LABEL_CHARS: int = 32
#: Tool-result body written when a turn is interrupted before every call
#: has a matching ``role=tool`` message. Providers reject the next request
#: if an assistant ``tool_calls`` entry is left unanswered.
INTERRUPTED_TOOL_RESULT: str = "interrupted by the user"
#: Heavy multi-agent tools hidden for simple (non-project) classified turns.
LITE_EXCLUDED_TOOLS: frozenset[str] = frozenset(
    {
        "Delegate",
        "Task",
        "Research",
        "Challenge",
        "Verify",
        "Orchestrate",
        "SpawnAgent",
        "SendMessage",
        "Inbox",
        "Team",
    }
)

# --------------------------------------------------------------------------
# Session storage
# --------------------------------------------------------------------------

#: Schema version written into each session's metadata.
SESSION_SCHEMA_VERSION: int = 1
#: Sessions listed by default.
SESSION_LIST_LIMIT: int = 50
#: Characters of the first user message used to title a session.
SESSION_TITLE_CHARS: int = 60
#: Messages buffered before the writer flushes to disk.
SESSION_FLUSH_INTERVAL: int = 1
#: Longest unsent composer draft kept on disk (characters).
COMPOSER_DRAFT_MAX_CHARS: int = 100_000
#: File extensions treated as previewable images in the GUI resource strip.
PREVIEWABLE_IMAGE_SUFFIXES: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
)
#: Largest single pasted/dragged image accepted (bytes).
ATTACHMENT_MAX_BYTES: int = 8 * 1024 * 1024
#: Cap on images attached to one user turn.
ATTACHMENT_MAX_COUNT: int = 32
#: Rough token estimate per image when metering context.
ATTACHMENT_TOKEN_ESTIMATE: int = 1100
#: MIME types the composer will accept as image attachments.
ATTACHMENT_ALLOWED_MIMES: frozenset[str] = frozenset(
    {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"}
)
#: Substrings in a vendor model id that usually mean vision is available.
#: Prefer setting ``supports_vision: true`` (or the ``vision`` tag) for
#: ambiguous ids — bare vendor names are too broad and cause API 400s.
VISION_MODEL_MARKERS: tuple[str, ...] = (
    "vision",
    "-vl",
    "vl-",
    "vl.",
    "4o",
    "gpt-4.1",
    "gpt-5",
    "gemini",
    "claude-3",
    "claude-4",
    "claude-sonnet",
    "claude-opus",
    "qwen-vl",
    "qwen2-vl",
    "qwen2.5-vl",
    # Kimi K3 (platform id + Coding Plan shorthand)
    "kimi-k3",
    "kimi_k3",
)
#: Exact model / alias ids that support vision even when short (Coding Plan
#: registers the flagship as bare ``k3``). Keep this set tight — substring
#: matching ``k3`` elsewhere would be too broad.
VISION_MODEL_IDS: frozenset[str] = frozenset({
    "k3",
    "kimi-k3",
    "kimi_k3",
})

# --------------------------------------------------------------------------
# Prompt caching
# --------------------------------------------------------------------------

#: Minimum stable prefix worth caching on most backends.
CACHE_MIN_PREFIX_TOKENS: int = 1024
#: Cache-hit ratio below which the status bar warns about prefix churn.
CACHE_HIT_WARN_THRESHOLD: float = 0.3

# --------------------------------------------------------------------------
# Scheduler
# --------------------------------------------------------------------------

#: Seconds between scheduler wake-ups.
SCHEDULER_TICK_SECONDS: float = 20.0
#: How long after its due time a missed run is still executed.
SCHEDULER_CATCHUP_GRACE_SECONDS: float = 300.0
#: Concurrent scheduled jobs.
SCHEDULER_MAX_CONCURRENT: int = 2
#: Runs retained in a job's history.
SCHEDULER_HISTORY_LIMIT: int = 20
#: Default turn budget for a scheduled job.
SCHEDULER_JOB_MAX_TURNS: int = 40

# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------

#: Maximum UI repaints per second while streaming.
UI_REFRESH_HZ: int = 20
#: Transcript entries kept in the scrollback before the head is dropped.
UI_SCROLLBACK_LIMIT: int = 2000
#: Characters of a tool result shown before it is collapsed behind a toggle.
UI_TOOL_PREVIEW_CHARS: int = 400
#: Chars of an injected steering note kept in the notice preview before "…".
UI_STEERING_PREVIEW_CHARS: int = 120
#: Slash-command suggestions offered at once.
UI_COMPLETION_LIMIT: int = 8
#: Width of the context-usage bar in the status line.
UI_CONTEXT_BAR_WIDTH: int = 16

# --------------------------------------------------------------------------
# Miscellaneous
# --------------------------------------------------------------------------

#: Tokens per million, used for pricing arithmetic.
TOKENS_PER_MILLION: int = 1_000_000
#: Seconds allowed for short-lived git introspection commands.
GIT_COMMAND_TIMEOUT: float = 5.0


# --------------------------------------------------------------------------
# HTTP status codes referenced by the provider adapter
# --------------------------------------------------------------------------

HTTP_OK: int = 200
HTTP_BAD_REQUEST: int = 400
HTTP_UNAUTHORIZED: int = 401
HTTP_FORBIDDEN: int = 403
HTTP_NOT_FOUND: int = 404
HTTP_TOO_MANY_REQUESTS: int = 429
#: Statuses a proxy answers with on the target's behalf when it cannot
#: reach it. Distinguishing these is what lets a failure be attributed to
#: the tunnel rather than to the endpoint.
HTTP_GATEWAY_ERRORS: tuple[int, ...] = (502, 503, 504)

#: API keys at or below this length are masked without showing a suffix.
MASKED_KEY_MIN_LENGTH: int = 10
#: Characters of an API key shown at each end when masking.
MASKED_KEY_PREFIX_CHARS: int = 6
MASKED_KEY_SUFFIX_CHARS: int = 4


# --------------------------------------------------------------------------
# Desktop control
# --------------------------------------------------------------------------

#: Seconds pyautogui pauses after each action, so the UI can keep up.
DESKTOP_ACTION_PAUSE: float = 0.15
#: Seconds between simulated keystrokes.
DESKTOP_TYPE_INTERVAL: float = 0.01
#: Longest string the agent may type in one call.
DESKTOP_MAX_TYPE_CHARS: int = 2000
#: Default wheel notches per scroll.
DESKTOP_SCROLL_CLICKS: int = 3
#: Workspace-relative directory screenshots are written to.
DESKTOP_SCREENSHOT_DIR: str = ".aiharness/screenshots"

# --------------------------------------------------------------------------
# Planning and task classification
# --------------------------------------------------------------------------

#: Complexity score at or above which a task is treated as a project.
#: Score at or above which a request is treated as a project and plan mode
#: is entered. This matches the classifier prompt's own band for "a project"
#: (8-10). It used to be 5, which is the prompt's "real work" band, so
#: ordinary multi-step tasks — and even research questions — were forced
#: through plan approval before anything could happen.
PROJECT_COMPLEXITY_THRESHOLD: int = 8
#: Score below which a task is answered directly with no ceremony.
TRIVIAL_COMPLEXITY_THRESHOLD: int = 2
#: Maximum clarifying questions asked in one round.
MAX_CLARIFYING_QUESTIONS: int = 4
#: Maximum options offered per clarifying question, excluding "other".
MAX_QUESTION_OPTIONS: int = 4
#: Plan steps beyond which the plan is considered too granular.
MAX_PLAN_STEPS: int = 20

# --------------------------------------------------------------------------
# Agent mesh
# --------------------------------------------------------------------------

#: Messages retained in one agent's inbox.
MAILBOX_LIMIT: int = 50
#: Characters of a single inter-agent message.
MAX_MESSAGE_CHARS: int = 8000
#: Concurrent child sessions one parent may run.
MAX_CHILD_SESSIONS: int = 6
#: Seconds a blocking send waits for a reply before giving up.
MESSAGE_REPLY_TIMEOUT: float = 300.0

# --------------------------------------------------------------------------
# Quest
# --------------------------------------------------------------------------

#: Automatic retries of the active step after a failed Verify, before the
#: Quest is blocked and the user must resume by hand. Zero disables retries.
QUEST_STEP_MAX_RETRIES: int = 2

# --------------------------------------------------------------------------
# Workflow learning
# --------------------------------------------------------------------------

#: Sessions scanned when mining for repeated workflows.
LEARNING_SESSION_LIMIT: int = 40
#: Times a pattern must recur before it is worth proposing as a skill.
LEARNING_MIN_OCCURRENCES: int = 3
#: Characters of each session summarised for the miner.
LEARNING_SESSION_DIGEST_CHARS: int = 3000
#: Elements in a screenshot region tuple: x, y, width, height.
SCREENSHOT_REGION_FIELDS: int = 4
#: Characters of typed text echoed back in the tool result.
DESKTOP_TYPE_PREVIEW_CHARS: int = 60
#: Maximum clicks accepted in one Click call.
DESKTOP_MAX_CLICKS: int = 3
#: A question needs at least this many options to be worth asking.
MIN_QUESTION_OPTIONS: int = 2

# --------------------------------------------------------------------------
# Built-in browser
# --------------------------------------------------------------------------

#: Seconds Playwright waits for navigation and selectors.
BROWSER_DEFAULT_TIMEOUT: float = 30.0
#: Interactive elements listed in one page snapshot.
BROWSER_MAX_ELEMENTS: int = 150
#: Characters of page text returned when the model asks for it.
BROWSER_MAX_TEXT_CHARS: int = 20000
#: Workspace-relative directory for page screenshots.
BROWSER_SCREENSHOT_DIR: str = ".aiharness/pages"
#: A session with fewer messages than this tells us nothing about habits.
LEARNING_MIN_SESSION_MESSAGES: int = 2

# --------------------------------------------------------------------------
# Market data and charts
# --------------------------------------------------------------------------

#: Bars shown on a chart by default.
MARKET_CHART_BARS: int = 90
#: Chart geometry, in terminal cells.
MARKET_CHART_WIDTH: int = 92
MARKET_CHART_HEIGHT: int = 16
MARKET_VOLUME_HEIGHT: int = 4
#: Hard cap on rows returned by MarketHistory.
MARKET_HISTORY_ROWS: int = 250
#: Hard cap on symbols screened in one call.
MARKET_SCREEN_LIMIT: int = 40
#: Default starting cash for the paper account, in yuan.
PAPER_INITIAL_CASH: float = 500_000.0


# --------------------------------------------------------------------------
# Session heartbeat
# --------------------------------------------------------------------------

#: Seconds between automatic continuations.
HEARTBEAT_DEFAULT_INTERVAL: float = 20.0
#: Bounds on the interval. Too short burns budget on half-finished turns;
#: too long stops feeling automatic.
HEARTBEAT_MIN_INTERVAL: float = 5.0
HEARTBEAT_MAX_INTERVAL: float = 600.0
#: Consecutive failed beats before the loop gives up.
HEARTBEAT_MAX_CONSECUTIVE_ERRORS: int = 5
#: Backoff between reconnect attempts, in seconds.
HEARTBEAT_RECONNECT_BACKOFF: float = 5.0
HEARTBEAT_RECONNECT_CEILING: float = 120.0


# --------------------------------------------------------------------------
# First-run setup
# --------------------------------------------------------------------------

#: Seconds allowed when fetching an account's model catalogue.
MODEL_FETCH_TIMEOUT: float = 20.0
#: Models offered at once when picking from an account's catalogue.
MODEL_LIST_LIMIT: int = 60
#: Window sizes offered when adding a model. The largest is the default:
#: a model that supports 1M should not silently be capped at 128k.
SETUP_CONTEXT_CHOICES: tuple[int, ...] = (
    32_768, 65_536, 131_072, 200_000, 262_144, 400_000, 1_000_000,
)
#: Context window assumed for a newly added model, until the user says otherwise.
SETUP_DEFAULT_CONTEXT: int = 1_000_000
#: Output cap assumed for a newly added model.
SETUP_DEFAULT_MAX_OUTPUT: int = 8192


# --------------------------------------------------------------------------
# Market display and formatting
# --------------------------------------------------------------------------

#: Minimum bars needed before a drawdown or period return means anything.
MIN_BARS_FOR_STATISTICS: int = 2
#: Price magnitudes at which the chart axis drops decimal places.
PRICE_LABEL_THOUSANDS: float = 1000.0
PRICE_LABEL_TENS: float = 10.0
#: Fields a qlib instrument line must have: symbol, start, end.
INSTRUMENT_LINE_FIELDS: int = 3

# --------------------------------------------------------------------------
# Number formatting
# --------------------------------------------------------------------------

#: Thresholds for rendering token counts as 1.2k / 3.4M.
THOUSAND: int = 1_000
MILLION: int = 1_000_000
#: Context share above which a breakdown row is emphasised.
BREAKDOWN_EMPHASIS_SHARE: float = 0.2
