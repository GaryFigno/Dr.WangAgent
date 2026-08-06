"""Configuration data model.

The central idea: a *model* and an *API account* are separate things.
One model (e.g. ``deepseek-chat``) may be reachable through several
accounts — same base_url with different keys, or entirely different
gateways.  The user picks ``model@account`` explicitly, or lets the
router pick for them.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from ..constants import (
    DEFAULT_MAX_AGENT_TURNS,
    MASKED_KEY_MIN_LENGTH,
    MASKED_KEY_PREFIX_CHARS,
    MASKED_KEY_SUFFIX_CHARS,
    REQUEST_TIMEOUT,
)

EffortMode = Literal["reasoning_effort", "thinking_budget", "temperature", "none"]
PermissionMode = Literal["ask", "auto", "yolo"]
RouteStrategy = Literal["priority", "round_robin", "least_used", "random"]


# --------------------------------------------------------------------------
# API accounts
# --------------------------------------------------------------------------


@dataclass
class ProviderAccount:
    """One set of credentials against one OpenAI-compatible endpoint.

    Two accounts on the same vendor simply share ``base_url`` and differ
    in ``api_key``; the router treats them as independent capacity.
    """

    id: str
    base_url: str
    #: The live key, used for requests. Never serialised when
    #: :attr:`api_key_env` is set.
    api_key: str = ""
    #: Name of the environment variable the key came from. When present, the
    #: config file stores ``${NAME}`` and never the secret itself — the two
    #: fields exist separately so that guarantee is structural rather than a
    #: convention somebody has to remember.
    api_key_env: str = ""
    #: How this account reaches the network. Blank follows the environment,
    #: "direct" ignores it, anything else is a proxy URL used only here.
    #: See :mod:`aiharness.providers.proxy`.
    proxy: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    # Lower number wins under the "priority" strategy.
    priority: int = 100
    # Soft ceiling used by the router to spread load; not enforced remotely.
    rpm_limit: int | None = None
    timeout: float = REQUEST_TIMEOUT
    # Sent verbatim in every request body (e.g. {"provider": {"sort": "throughput"}}).
    extra_body: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    note: str = ""

    def for_storage(self) -> ProviderAccount:
        """A copy safe to write to disk.

        The key is *always* stripped, because there is no case where a secret
        belongs in ``config.yaml`` — people commit it, paste it into issues
        and sync it between machines. What replaces it depends on where the
        key came from:

        * from an environment variable, the copy carries ``${NAME}``, which
          :func:`~aiharness.config.loader.load_config` expands on the way
          back in;
        * pasted, the field is left empty and the key is read from the
          credential store by account id instead.

        Blanking unconditionally is deliberate. Redacting only the cases we
        thought of is how a key ends up on disk the first time somebody adds
        a fourth way to supply one.
        """
        import copy

        stored = copy.deepcopy(self)
        stored.api_key = f"${{{self.api_key_env}}}" if self.api_key_env else ""
        return stored

    def masked_key(self) -> str:
        """Render the key for display without revealing it."""
        if not self.api_key:
            return "<none>"
        if len(self.api_key) <= MASKED_KEY_MIN_LENGTH:
            return self.api_key[:2] + "***"
        head = self.api_key[:MASKED_KEY_PREFIX_CHARS]
        tail = self.api_key[-MASKED_KEY_SUFFIX_CHARS:]
        return f"{head}...{tail}"


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------


@dataclass
class EffortSpec:
    """How to express "thinking effort" for this model.

    Every vendor spells it differently, so the mapping lives in config
    instead of being hardcoded per model name.
    """

    mode: EffortMode = "none"
    # Logical level -> vendor value. Value type depends on mode:
    #   reasoning_effort -> str ("low"/"medium"/"high")
    #   thinking_budget  -> int (token budget)
    #   temperature      -> float
    levels: dict[str, Any] = field(
        default_factory=lambda: {"low": "low", "medium": "medium", "high": "high"}
    )
    # Key name in the request body; overrides the mode's default key.
    param_name: str | None = None
    # Where the parameter goes: top level, or nested under another key.
    nest_under: str | None = None

    def default_param_name(self) -> str:
        if self.param_name:
            return self.param_name
        return {
            "reasoning_effort": "reasoning_effort",
            "thinking_budget": "thinking_budget",
            "temperature": "temperature",
        }.get(self.mode, "reasoning_effort")

    def build(self, level: str) -> dict[str, Any]:
        """Turn a logical effort level into request-body fragments."""
        if self.mode == "none" or level not in self.levels:
            return {}
        value = self.levels[level]
        key = self.default_param_name()
        if self.nest_under:
            return {self.nest_under: {key: value}}
        return {key: value}


@dataclass
class Pricing:
    """USD per 1M tokens, used only for local cost accounting."""

    input: float = 0.0
    output: float = 0.0
    cached_input: float | None = None


@dataclass
class ModelDef:
    id: str  # local display name, must be unique
    model: str  # the string actually sent as "model"
    accounts: list[str] = field(default_factory=list)  # ProviderAccount ids
    # Selectable context sizes. The first is used when default_context is unset.
    context_windows: list[int] = field(default_factory=lambda: [128000])
    default_context: int | None = None
    max_output_tokens: int = 8192
    supports_tools: bool = True
    supports_streaming: bool = True
    #: Cached detection result (API probe / name heuristics). Used when
    #: :attr:`vision_mode` is ``auto``.
    supports_vision: bool = False
    #: ``auto`` = probe/heuristic; ``on`` / ``off`` = user lock (wins).
    vision_mode: str = "auto"
    # Some reasoning models reject temperature/top_p entirely.
    supports_temperature: bool = True
    effort: EffortSpec = field(default_factory=EffortSpec)
    default_effort: str = "medium"
    pricing: Pricing = field(default_factory=Pricing)
    # Merged into every request for this model.
    extra_body: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)  # e.g. ["cheap", "fast"]
    note: str = ""

    def context_for(self, requested: int | None = None) -> int:
        if requested:
            return requested
        if self.default_context:
            return self.default_context
        return self.context_windows[0] if self.context_windows else 128000

    def effort_levels(self) -> list[str]:
        if self.effort.mode == "none":
            return []
        return list(self.effort.levels.keys())


# --------------------------------------------------------------------------
# Roles — which model does which job
# --------------------------------------------------------------------------


@dataclass
class RoleBinding:
    """Binds a logical job ("cheap", "verifier", ...) to a concrete model.

    ``account`` pins a specific API account; leave it empty to let the
    router choose among the model's accounts.
    """

    model: str
    account: str | None = None
    effort: str | None = None
    context: int | None = None
    temperature: float | None = None

    def describe(self) -> str:
        s = self.model
        if self.account:
            s += f"@{self.account}"
        if self.effort:
            s += f" [{self.effort}]"
        return s


# Roles the harness itself looks up. Extra user-defined roles are allowed.
BUILTIN_ROLES = [
    "main",  # drives the conversation
    "fast",  # quick turnarounds, cheap-ish, still capable
    "cheap",  # bulk/trivial work: summarising, file triage, commit messages
    "verifier",  # checks the main model's output
    "adversary",  # attacks the main model's output
    "researcher",  # parallel investigation subagents
    "compactor",  # context compaction summaries
    "titler",  # session titles, tiny labels
]


# --------------------------------------------------------------------------
# MCP servers
# --------------------------------------------------------------------------


@dataclass
class MCPServerConfig:
    """One Model Context Protocol server.

    Set ``command`` for a local server run as a child process, or ``url`` for
    a hosted one. Its tools appear to the agent as ``mcp__<id>__<tool>``.
    """

    id: str
    #: Local server: the executable and its arguments.
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    #: Hosted server: the streamable-HTTP endpoint.
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    timeout: float = 60.0
    #: When set, only these tool names are exposed.
    tools_allow: list[str] = field(default_factory=list)
    #: Tool names never exposed, regardless of tools_allow.
    tools_deny: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def is_remote(self) -> bool:
        return bool(self.url)

    def describe(self) -> str:
        if self.url:
            return self.url
        return " ".join([self.command, *self.args]).strip()


# --------------------------------------------------------------------------
# Desktop control
# --------------------------------------------------------------------------


@dataclass
class DesktopConfig:
    """Screen, mouse and keyboard control.

    Off by default, and deliberately so: unlike every other tool here, these
    act outside the workspace, and the agent decides what to click by reading
    pixels it did not produce. See :mod:`aiharness.tools.computer`.
    """

    enabled: bool = False
    #: Refuse desktop actions unless the permission mode is at least this
    #: strict. "ask" means every click is confirmed.
    require_mode: PermissionMode = "ask"
    #: Keep every screenshot the agent takes, for after-the-fact review.
    keep_screenshots: bool = True


# --------------------------------------------------------------------------
# Built-in browser
# --------------------------------------------------------------------------


@dataclass
class BrowserConfig:
    """The bundled Playwright browser.

    Off by default because it launches Chromium and reads the open web —
    both of which the user should opt into rather than discover.
    """

    enabled: bool = False
    #: Run without a visible window. Off by default so the user can watch.
    headless: bool = False
    timeout: float = 30.0
    #: When non-empty, only these domains may be visited (suffix match).
    allow_domains: list[str] = field(default_factory=list)
    #: Domains never visited, checked before the allow list.
    deny_domains: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Market data
# --------------------------------------------------------------------------


@dataclass
class MarketConfig:
    """Price data, charts and the paper account.

    ``qlib_store`` points at an existing qlib binary store — the one a
    backtesting setup already maintains. Reading that store rather than
    fetching fresh data means an analysis and a backtest cannot quietly
    disagree about what the prices were.
    """

    enabled: bool = False
    #: Path to a qlib store, or its parent. Empty disables local history.
    qlib_store: str = ""
    #: Allow akshare live quotes. Off means the harness never hits the network
    #: for prices.
    live_quotes: bool = True
    #: Starting cash for the paper account, in yuan.
    paper_cash: float = 500_000.0
    #: Where the paper account is kept; relative paths resolve in the workspace.
    paper_file: str = ".aiharness/paper.json"
    #: Symbols surfaced in the market panel and screened by default.
    watchlist: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------


@dataclass
class PlanningConfig:
    """How the agent decides whether a request is a chore or a project."""

    enabled: bool = True
    #: Classify each new request and route it accordingly.
    auto_classify: bool = True
    #: Ask clarifying questions when the request is ambiguous.
    ask_when_unclear: bool = True
    #: Require the user to approve a plan before any file is written.
    require_plan_approval: bool = True
    #: Model used for classification and plan drafting.
    planner_role: str = "main"
    #: Model used to classify complexity; cheap is usually enough.
    classifier_role: str = "fast"


# --------------------------------------------------------------------------
# Permissions
# --------------------------------------------------------------------------


@dataclass
class PermissionConfig:
    mode: PermissionMode = "ask"
    # Rule syntax: "Tool(pattern)" e.g. "Bash(git status:*)", "Read(*)".
    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    # Always prompt for these, even in auto/yolo (after deny/allow).
    ask: list[str] = field(default_factory=list)
    # Directories the agent may touch. Empty means "the workspace only".
    additional_directories: list[str] = field(default_factory=list)
    # Even in yolo mode these are refused. Turn off at your own risk.
    block_catastrophic: bool = True
    # Seconds before an unanswered permission prompt auto-denies. 0 = wait forever
    # (GUI still has a long safety ceiling).
    prompt_timeout: float = 0.0


# --------------------------------------------------------------------------
# Workflows
# --------------------------------------------------------------------------


@dataclass
class AdversarialConfig:
    enabled: bool = False
    rounds: int = 2
    adversary_role: str = "adversary"
    # Stop early when the adversary reports no blocking issues.
    stop_when_clean: bool = True
    # Run automatically after the main model finishes a turn that wrote files.
    auto_trigger: bool = False


@dataclass
class VerifyConfig:
    enabled: bool = True
    verifier_role: str = "verifier"
    # Shell commands run as ground truth before the model judges.
    commands: list[str] = field(default_factory=list)
    auto_trigger: bool = False
    max_fix_attempts: int = 2


@dataclass
class ResearchConfig:
    # How many subagents run at once.
    parallel: int = 3
    # Models used for the fan-out; falls back to the "researcher" role.
    models: list[RoleBinding] = field(default_factory=list)
    synthesis_role: str = "main"
    max_turns: int = 12


@dataclass
class DelegationConfig:
    """Automatic routing of simple work to a cheaper model."""

    enabled: bool = True
    cheap_role: str = "cheap"
    fast_role: str = "fast"
    # Tools whose internal LLM calls always go to the cheap model.
    always_cheap_tools: list[str] = field(
        default_factory=lambda: ["Grep", "Glob", "Read"]
    )
    # The main model can hand a whole task to the cheap model via this tool.
    expose_delegate_tool: bool = True


@dataclass
class WorkflowConfig:
    adversarial: AdversarialConfig = field(default_factory=AdversarialConfig)
    verify: VerifyConfig = field(default_factory=VerifyConfig)
    research: ResearchConfig = field(default_factory=ResearchConfig)
    delegation: DelegationConfig = field(default_factory=DelegationConfig)


# --------------------------------------------------------------------------
# Context management
# --------------------------------------------------------------------------


@dataclass
class ContextConfig:
    # Compact once this fraction of the window is used.
    compact_threshold: float = 0.82
    # Fraction of the window the compacted summary may occupy.
    summary_budget: float = 0.15
    # Never compact away the last N messages.
    keep_recent_messages: int = 8
    # Prefer keeping about this many tokens of recent transcript verbatim
    # (grows the recent tail beyond keep_recent_messages when cheap).
    preserve_recent_tokens: int = 8_000
    # Truncate any single tool result longer than this many characters.
    max_tool_result_chars: int = 30000
    auto_compact: bool = True
    # Shrink older tool outputs before paying for an LLM summary.
    prune_tool_outputs: bool = True
    # Assumed window when a model definition is missing or incomplete.
    fallback_window: int = 1_000_000


@dataclass
class UIConfig:
    theme: str = "dark"
    show_reasoning: bool = True
    show_token_counts: bool = True
    show_cost: bool = True
    stream: bool = True
    #: When True and permission mode is yolo, Write/Edit skip the review queue.
    auto_apply_edits: bool = False


# --------------------------------------------------------------------------
# Root
# --------------------------------------------------------------------------


@dataclass
class Config:
    accounts: list[ProviderAccount] = field(default_factory=list)
    models: list[ModelDef] = field(default_factory=list)
    roles: dict[str, RoleBinding] = field(default_factory=dict)
    permissions: PermissionConfig = field(default_factory=PermissionConfig)
    workflows: WorkflowConfig = field(default_factory=WorkflowConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    mcp_servers: list[MCPServerConfig] = field(default_factory=list)
    desktop: DesktopConfig = field(default_factory=DesktopConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    planning: PlanningConfig = field(default_factory=PlanningConfig)
    market: MarketConfig = field(default_factory=MarketConfig)
    route_strategy: RouteStrategy = "priority"
    # Extra directories scanned for SKILL.md files.
    skill_paths: list[str] = field(default_factory=list)
    system_prompt_append: str = ""
    max_agent_turns: int = DEFAULT_MAX_AGENT_TURNS

    # -- lookups ----------------------------------------------------------

    def account(self, account_id: str) -> ProviderAccount | None:
        return next((a for a in self.accounts if a.id == account_id), None)

    def mcp_server(self, server_id: str) -> MCPServerConfig | None:
        return next((s for s in self.mcp_servers if s.id == server_id), None)

    def model(self, model_id: str) -> ModelDef | None:
        return next((m for m in self.models if m.id == model_id), None)

    def accounts_for(self, model_id: str) -> list[ProviderAccount]:
        m = self.model(model_id)
        if not m:
            return []
        out = []
        for aid in m.accounts:
            acc = self.account(aid)
            if acc and acc.enabled:
                out.append(acc)
        return out

    def role(self, name: str) -> RoleBinding | None:
        binding = self.roles.get(name)
        if binding:
            return binding
        # Graceful degradation: any unbound role falls back to main.
        return self.roles.get("main")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def validate(self) -> list[str]:
        """Return a list of human-readable problems (empty means OK)."""
        problems: list[str] = []
        account_ids = {a.id for a in self.accounts}
        model_ids = {m.id for m in self.models}

        if len(account_ids) != len(self.accounts):
            problems.append("duplicate account ids")
        if len(model_ids) != len(self.models):
            problems.append("duplicate model ids")

        for m in self.models:
            if not m.accounts:
                problems.append(f"model '{m.id}' has no accounts")
            for aid in m.accounts:
                if aid not in account_ids:
                    problems.append(f"model '{m.id}' references unknown account '{aid}'")
            if m.default_context and m.context_windows:
                if m.default_context > max(m.context_windows):
                    problems.append(
                        f"model '{m.id}' default_context exceeds its largest context_window"
                    )
            if m.default_effort and m.effort.mode != "none":
                if m.default_effort not in m.effort.levels:
                    problems.append(
                        f"model '{m.id}' default_effort '{m.default_effort}' is not a defined level"
                    )

        for name, binding in self.roles.items():
            if binding.model not in model_ids:
                problems.append(f"role '{name}' references unknown model '{binding.model}'")
            if binding.account and binding.account not in account_ids:
                problems.append(f"role '{name}' references unknown account '{binding.account}'")

        if "main" not in self.roles:
            problems.append("no 'main' role configured")

        for a in self.accounts:
            if a.enabled and not a.api_key and "localhost" not in a.base_url and "127.0.0.1" not in a.base_url:
                problems.append(f"account '{a.id}' has no api_key (unresolved env var?)")

        seen_servers: set[str] = set()
        for server in self.mcp_servers:
            if server.id in seen_servers:
                problems.append(f"duplicate MCP server id '{server.id}'")
            seen_servers.add(server.id)
            if not server.command and not server.url:
                problems.append(f"MCP server '{server.id}' needs either a command or a url")
            if server.command and server.url:
                problems.append(f"MCP server '{server.id}' sets both command and url; pick one")

        return problems
