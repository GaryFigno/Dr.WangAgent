"""Slash commands.

Each handler receives the running app and the raw argument string, and
returns text to show in the transcript (or ``None`` when it has already
updated the UI itself). Handlers may be coroutines.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from ..config.schema import PermissionMode
from ..providers.openai_compat import probe_account
from ..providers.router import NoRouteError, Selection
from ..scheduler.cron import Schedule, ScheduleError, parse_weekday
from .theme import THEMES, get_theme, theme_names

if TYPE_CHECKING:  # pragma: no cover
    from .app import HarnessApp

Handler = Callable[["HarnessApp", str], "str | None | Awaitable[str | None]"]

#: Hours-to-minutes, for "every 4h" style intervals.
MINUTES_PER_HOUR = 60
#: `weekly`/`every`/`cron` need at least a keyword plus one argument.
SCHEDULE_MIN_TOKENS = 2
#: `/job add` takes name | schedule | prompt.
JOB_ADD_FIELD_COUNT = 3
#: Fields required by `/accounts add`: id, url, env var name.
ACCOUNT_ADD_FIELDS = 3
#: Models listed at once when picking from an account's catalogue.
MODEL_PICK_LIMIT = 40
#: `/role` takes a role name and a model spec.
ROLE_ARG_COUNT = 2
#: `/models add <account> <model-id> [alias]`: the alias is the third arg.
ALIAS_ARG_INDEX = 2
#: Permission modes accepted by /mode.
VALID_MODES: tuple[PermissionMode, ...] = ("ask", "auto", "yolo")


@dataclass
class Command:
    name: str
    summary: str
    usage: str
    handler: Handler
    aliases: tuple[str, ...] = ()


REGISTRY: dict[str, Command] = {}


def command(name: str, summary: str, usage: str = "", *aliases: str) -> Callable[[Handler], Handler]:
    """Register a slash command."""

    def decorate(handler: Handler) -> Handler:
        entry = Command(name, summary, usage or f"/{name}", handler, aliases)
        REGISTRY[name] = entry
        for alias in aliases:
            REGISTRY[alias] = entry
        return handler

    return decorate


async def dispatch(app: HarnessApp, line: str) -> str | None:
    """Run a slash command line.

    Args:
      app: The running application.
      line: The full input, beginning with ``/``.

    Returns:
      Text to display, or ``None`` when the handler already rendered output.
    """
    body = line[1:].strip()
    if not body:
        return None
    name, _, args = body.partition(" ")
    entry = REGISTRY.get(name.lower())
    if entry is None:
        near = suggest(name)
        hint = f" Did you mean {', '.join('/' + n for n in near)}?" if near else ""
        return f"Unknown command /{name}.{hint} Try /help."
    result = entry.handler(app, args.strip())
    if inspect.isawaitable(result):
        result = await result
    return result


def suggest(prefix: str, limit: int = 5) -> list[str]:
    """Return command names starting with ``prefix``."""
    prefix = prefix.lower()
    seen: list[str] = []
    for name, entry in REGISTRY.items():
        if name.startswith(prefix) and entry.name not in seen:
            seen.append(entry.name)
    return sorted(seen)[:limit]


def completions(line: str, limit: int) -> list[tuple[str, str]]:
    """Return (name, summary) pairs matching a partial slash command."""
    if not line.startswith("/"):
        return []
    prefix = line[1:].split(" ", maxsplit=1)[0].lower()
    unique: dict[str, Command] = {}
    for name, entry in REGISTRY.items():
        if name.startswith(prefix):
            unique[entry.name] = entry
    ordered = sorted(unique.values(), key=lambda c: c.name)
    return [(f"/{c.name}", c.summary) for c in ordered[:limit]]


# --------------------------------------------------------------------------
# help and diagnostics
# --------------------------------------------------------------------------


@command("help", "List commands", "/help [command]", "h", "?")
def _help(app: HarnessApp, args: str) -> str:
    if args:
        entry = REGISTRY.get(args.strip().lstrip("/").lower())
        if entry is None:
            return f"No such command: {args}"
        return f"**{entry.usage}**\n\n{entry.summary}"
    seen: dict[str, Command] = {c.name: c for c in REGISTRY.values()}
    lines = ["**Commands**", ""]
    for entry in sorted(seen.values(), key=lambda c: c.name):
        lines.append(f"`{entry.usage}` — {entry.summary}")
    lines += [
        "",
        "**Keys** — `ctrl+c` interrupt · `ctrl+d` quit · `ctrl+b` sidebar · "
        "`ctrl+r` show/hide reasoning · `ctrl+l` clear screen",
    ]
    return "\n".join(lines)


@command("doctor", "Check every API account's credentials", "/doctor")
async def _doctor(app: HarnessApp, args: str) -> str:
    problems = app.config.validate()
    lines = ["**Configuration**", ""]
    lines.append("no problems found" if not problems else "\n".join(f"- {p}" for p in problems))
    lines += ["", "**Accounts**", ""]
    for account in app.config.accounts:
        if not account.enabled:
            lines.append(f"- {account.id}: disabled")
            continue
        ok, detail = await probe_account(account)
        glyph = "✓" if ok else "✗"
        lines.append(f"- {glyph} {account.id} ({account.base_url}) key={account.masked_key()} — {detail}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# model, account, effort, context
# --------------------------------------------------------------------------


@command("models", "List or add models", "/models [add <account> [model-id]]")
async def _models(app: HarnessApp, args: str) -> str:
    action, _, rest = args.strip().partition(" ")
    if action == "add":
        return await _models_add(app, rest.strip())
    if action in ("rm", "remove", "delete"):
        return _models_remove(app, rest.strip())
    return _models_list(app)


async def _models_add(app: HarnessApp, rest: str) -> str:
    """Offer only what the named account actually serves."""
    from ..setup import SetupError, build_model, probe_and_list, suggest_alias

    parts = rest.split()
    if not parts:
        known = ", ".join(a.id for a in app.config.accounts) or "(none)"
        return f"Usage: `/models add <account> [model-id] [alias]`. Accounts: {known}"

    account = app.config.account(parts[0])
    if account is None:
        return f"No account `{parts[0]}`. Add one with `/accounts add`."

    if len(parts) == 1:
        app._notice(f"fetching the model list from {account.id}...")
        probe = await probe_and_list(account)
        if not probe.ok:
            return f"Could not reach {account.id}: {probe.detail}"
        if not probe.chat_models:
            return (
                f"{account.id} did not list any models ({probe.detail}). "
                f"Add one by id: `/models add {account.id} <model-id>`"
            )
        listing = "\n".join(f"- `{m}`" for m in probe.chat_models[:MODEL_PICK_LIMIT])
        extra = len(probe.chat_models) - MODEL_PICK_LIMIT
        more = f"\n\n_({extra} more not shown)_" if extra > 0 else ""
        return (
            f"**{account.id} serves {len(probe.chat_models)} chat model(s):**\n\n"
            f"{listing}{more}\n\n"
            f"Add one with `/models add {account.id} <model-id>`."
        )

    model_id = parts[1]
    existing = {m.id for m in app.config.models}
    alias = parts[2] if len(parts) > ALIAS_ARG_INDEX else suggest_alias(model_id, existing)
    if alias in existing:
        return f"`{alias}` is already configured. Pick another alias."

    try:
        model = build_model(alias, model_id, [account.id])
    except SetupError as error:
        return str(error)
    app.config.models.append(model)

    hint = ""
    if "main" not in app.config.roles:
        hint = f"\n\nNothing is assigned to `main` yet: `/role main {alias}`"
    effort_note = " with effort levels" if model.effort.mode != "none" else ""
    return f"Added **{alias}** -> `{model_id}` on `{account.id}`{effort_note}.{hint}"


def _models_remove(app: HarnessApp, alias: str) -> str:
    model = app.config.model(alias)
    if model is None:
        return f"No model `{alias}`."
    bound = [role for role, b in app.config.roles.items() if b.model == alias]
    if bound:
        return f"`{alias}` is still bound to: {', '.join(bound)}. Reassign those roles first."
    app.config.models.remove(model)
    return f"Removed `{alias}`."


def _models_list(app: HarnessApp) -> str:
    if not app.config.models:
        return SETUP_HELP
    lines = ["**Models**", ""]
    for model in app.config.models:
        accounts = ", ".join(model.accounts) or "(none)"
        efforts = ", ".join(model.effort_levels()) or "n/a"
        windows = ", ".join(f"{w:,}" for w in model.context_windows)
        active = " ←" if model.id == app.agent.selection.model_id else ""
        lines.append(
            f"- **{model.id}** → `{model.model}`{active}\n"
            f"  accounts: {accounts}\n"
            f"  context: {windows} (default {model.context_for():,})\n"
            f"  effort: {efforts} · ${model.pricing.input}/${model.pricing.output} per 1M"
        )
    return "\n".join(lines)


SETUP_HELP = """\
**Setup** — nothing is configured until you say so.

The harness ships with no models and reads no keys it was not told about. Fill
it in with three steps:

**1. Add an account.** The key is given as an *environment variable name*, so
it never lands in the config file.

    /accounts add ds https://api.deepseek.com/v1 DEEPSEEK_API_KEY

The endpoint is contacted immediately; if the credentials are rejected,
nothing is saved.

**2. Add models.** The list comes from that account, so only models it can
actually serve are offerable.

    /models add ds

**3. Assign roles.** `main` is the only required one; the rest fall back to it.

    /role main reasoner
    /role cheap mini@ds

`/setup` shows what is still missing. `/config save` writes it to disk.
"""


@command("setup", "Show what still needs configuring", "/setup")
def _setup(app: HarnessApp, args: str) -> str:
    from ..setup import readiness, role_table

    ready, problems = readiness(app.config)
    if not app.config.accounts:
        return SETUP_HELP

    lines = ["**Setup**", ""]
    lines.append("Ready to run." if ready else "**Not ready yet:**")
    if problems:
        lines += [f"- {problem}" for problem in problems]
    lines += ["", "**Accounts**", ""]
    for account in app.config.accounts:
        served = [m.id for m in app.config.models if account.id in m.accounts]
        lines.append(
            f"- `{account.id}` {account.base_url} · key {account.masked_key()} · "
            f"models: {', '.join(served) or '(none yet — /models add ' + account.id + ')'}"
        )
    lines += ["", "**Roles**", ""]
    for role, binding, explicit in role_table(app.config):
        marker = "" if explicit else "  _(inherited)_"
        lines.append(f"- `{role}` → {binding}{marker}")
    lines += ["", "`/config save` writes this to disk.", "", SETUP_HELP]
    return "\n".join(lines)


@command("config", "Save or show the configuration file", "/config [save|path]")
def _config(app: HarnessApp, args: str) -> str:
    from ..config.loader import default_config_path, save_config

    action = args.strip().lower()
    if action == "save":
        problems = app.config.validate()
        if problems:
            listed = "\n".join(f"- {p}" for p in problems)
            return f"Not saving — the configuration has problems:\n\n{listed}"
        path = save_config(app.config)
        return f"Wrote `{path}`."
    return (
        f"Config file: `{default_config_path()}`\n\n"
        f"{len(app.config.accounts)} account(s), {len(app.config.models)} model(s), "
        f"{len(app.config.roles)} role(s) assigned.\n\n"
        f"`/config save` writes the current in-memory configuration there."
    )


@command("role", "Assign a model to a role", "/role <role> <model[@account]>", "roles")
def _role(app: HarnessApp, args: str) -> str:
    from ..setup import SetupError, assign_role, role_table

    parts = args.split()
    if not parts:
        lines = ["**Roles**", ""]
        for role, binding, explicit in role_table(app.config):
            marker = "" if explicit else "  _(falls back to main)_"
            lines.append(f"- `{role}` → {binding}{marker}")
        models = ", ".join(m.id for m in app.config.models) or "(none configured)"
        lines += ["", f"Assign with `/role <role> <model>`. Available models: {models}"]
        return "\n".join(lines)

    if len(parts) < ROLE_ARG_COUNT:
        return "Usage: `/role <role> <model[@account]>`"

    try:
        binding = assign_role(app.config, parts[0], parts[1])
    except SetupError as error:
        return str(error)
    if parts[0] == "main":
        app.set_selection(Selection.from_binding(binding))
    return f"`{parts[0]}` → **{binding.describe()}**. `/config save` to keep it."


@command("accounts", "List or add API accounts", "/accounts [add <id> <url> <ENV_VAR>]")
async def _accounts(app: HarnessApp, args: str) -> str:
    action, _, rest = args.strip().partition(" ")
    if action == "add":
        return await _accounts_add(app, rest.strip())
    if action in ("rm", "remove", "delete"):
        return _accounts_remove(app, rest.strip())
    return _accounts_list(app)


async def _accounts_add(app: HarnessApp, rest: str) -> str:
    """Add an account after checking it against the live endpoint."""
    import os

    from ..setup import SetupError, build_account, probe_and_list

    parts = rest.split()
    if len(parts) < ACCOUNT_ADD_FIELDS:
        return (
            "Usage: `/accounts add <id> <base_url> <ENV_VAR_NAME>`\n\n"
            "The third argument is the *name of an environment variable* "
            "holding the key, not the key itself — so it never reaches the "
            "config file.\n\n"
            "    /accounts add ds https://api.deepseek.com/v1 DEEPSEEK_API_KEY"
        )

    account_id, base_url, env_var = parts[0], parts[1], parts[2]
    if app.config.account(account_id) is not None:
        return f"An account called `{account_id}` already exists."

    key = os.environ.get(env_var, "")
    if not key:
        return (
            f"`{env_var}` is not set in this environment. Set it and restart, "
            f"or pass the name of a variable that is set."
        )

    try:
        account = build_account(account_id, base_url, key, api_key_env=env_var)
    except SetupError as error:
        return str(error)

    app._notice(f"contacting {account.base_url}…")
    probe = await probe_and_list(account)
    if not probe.ok:
        return f"Not saved — {account.base_url} said: {probe.detail}"

    # The live object keeps the real key; `for_storage()` swaps in the
    # ${ENV} reference at save time, so the secret never reaches disk.
    app.config.accounts.append(account)

    listing = ""
    if probe.chat_models:
        preview = ", ".join(probe.chat_models[:8])
        listing = f"\n\nIt serves {len(probe.chat_models)} chat model(s): {preview}…"
    return (
        f"Added **{account_id}** — {probe.detail}.{listing}\n\n"
        f"Next: `/models add {account_id}`"
    )


def _accounts_remove(app: HarnessApp, account_id: str) -> str:
    account = app.config.account(account_id)
    if account is None:
        return f"No account `{account_id}`."
    users = [m.id for m in app.config.models if account_id in m.accounts]
    if users:
        return (
            f"`{account_id}` still serves {', '.join(users)}. "
            f"Remove those models first, or point them at another account."
        )
    app.config.accounts.remove(account)
    return f"Removed `{account_id}`. `/config save` to keep it."


def _accounts_list(app: HarnessApp) -> str:
    if not app.config.accounts:
        return SETUP_HELP
    lines = ["**API accounts**", ""]
    for account in app.config.accounts:
        state = app.router.state(account.id)
        status = "disabled" if not account.enabled else "ok"
        if account.enabled and state.cooldown_until > 0:
            import time

            remaining = state.cooldown_until - time.time()
            if remaining > 0:
                status = f"cooling down {remaining:.0f}s"
        served = [m.id for m in app.config.models if account.id in m.accounts]
        lines.append(
            f"- **{account.id}** {account.base_url}\n"
            f"  key {account.masked_key()} · priority {account.priority} · "
            f"{state.total_calls} call(s), {state.failures} failure(s) · {status}\n"
            f"  serves: {', '.join(served) or '(no models yet)'}"
        )
    return "\n".join(lines)


@command("model", "Switch model, optionally pinning an API account", "/model [model[@account]]", "m")
def _model(app: HarnessApp, args: str) -> str:
    if not args:
        current = app.agent.selection
        return (
            f"Current: **{current.label()}**\n\n"
            f"Switch with `/model <model>` or pin an account with "
            f"`/model <model>@<account>`. See `/models`."
        )
    try:
        selection = Selection.parse(args, app.config)
    except NoRouteError as error:
        return str(error)
    # Carry over the user's effort/context choices unless the spec set them.
    selection.effort = selection.effort or app.agent.selection.effort
    selection.context = selection.context or None
    app.set_selection(selection)
    return f"Switched to **{selection.label()}**."


@command("effort", "Set reasoning effort for the current model", "/effort [low|medium|high]", "e")
def _effort(app: HarnessApp, args: str) -> str:
    model = app.config.model(app.agent.selection.model_id)
    if model is None:
        return "No model selected."
    levels = model.effort_levels()
    if not levels:
        return f"**{model.id}** does not expose an effort parameter (effort.mode is `none`)."
    if not args:
        current = app.agent.selection.effort or model.default_effort
        return f"Effort: **{current}**. Available: {', '.join(levels)}."
    level = args.strip().lower()
    if level not in levels:
        return f"Unknown effort '{level}'. Available: {', '.join(levels)}."
    selection = app.agent.selection
    selection.effort = level
    app.set_selection(selection)
    return f"Effort set to **{level}**."


@command("context", "Set the context window size for this session", "/context [tokens]", "ctx")
def _context(app: HarnessApp, args: str) -> str:
    model = app.config.model(app.agent.selection.model_id)
    if model is None:
        return "No model selected."
    if not args:
        app.show_context_breakdown()
        options = ", ".join(f"{w:,}" for w in model.context_windows)
        return (
            f"Selectable window sizes for **{model.id}**: {options}.\n\n"
            f"Resize with `/context <tokens>`. `ctrl+g` toggles this panel."
        )
    if args.strip().lower() in ("hide", "off"):
        app.query_one("#context-panel").remove_class("visible")
        return None
    try:
        requested = int(args.strip().replace(",", "").replace("k", "000"))
    except ValueError:
        return f"Not a number: {args}"
    largest = max(model.context_windows) if model.context_windows else requested
    if requested > largest:
        return f"**{model.id}** tops out at {largest:,} tokens."
    selection = app.agent.selection
    selection.context = requested
    app.set_selection(selection)
    return f"Context window set to **{requested:,}** tokens."


# --------------------------------------------------------------------------
# permissions
# --------------------------------------------------------------------------


@command("mode", "Set the permission mode", "/mode [ask|auto|yolo]")
def _mode(app: HarnessApp, args: str) -> str:
    if not args:
        rules = app.permissions.session_rules()
        extra = f"\n\nSession allow rules: {', '.join(rules)}" if rules else ""
        return f"Permission mode: **{app.permissions.mode}**.{extra}"
    mode = args.strip().lower()
    if mode not in VALID_MODES:
        return f"Mode must be one of: {', '.join(VALID_MODES)}."
    app.permissions.set_mode(mode)  # type: ignore[arg-type]
    app.session.set_permission_mode(mode)
    app.config.permissions.mode = mode  # type: ignore[assignment]
    app.agent.invalidate_system_prompt()
    warning = ""
    if mode == "yolo":
        warning = (
            "\n\nNothing will prompt from now on. Catastrophic commands are still "
            "refused while `block_catastrophic` is on."
        )
    return f"Permission mode set to **{mode}**.{warning}"


@command("allow", "Add an allow rule for this session", "/allow Bash(git push:*)")
def _allow(app: HarnessApp, args: str) -> str:
    if not args:
        rules = app.permissions.session_rules()
        return "Session rules: " + (", ".join(rules) if rules else "(none)")
    if app.permissions.allow_for_session(args.strip()):
        return f"Allowed for this session: `{args.strip()}`"
    return f"Could not parse rule: {args}. Use the form `Tool(pattern)`."


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------


@command("plan", "Enter or leave plan mode", "/plan [on|off]")
def _plan(app: HarnessApp, args: str) -> str:
    choice = args.strip().lower()
    if choice in ("off", "exit", "stop"):
        app.set_plan_mode(False)
        return "Left plan mode. Writes are unblocked."
    if choice in ("", "on", "start"):
        app.set_plan_mode(True)
        return (
            "**Plan mode.** Every write is blocked until you approve a plan — "
            "including in yolo mode. Describe what you want; the agent will "
            "investigate, then propose. `/plan off` leaves without a plan."
        )
    return "Usage: `/plan [on|off]`"


@command("explore", "Enter or leave read-only explore mode", "/explore [on|off]")
def _explore(app: HarnessApp, args: str) -> str:
    choice = args.strip().lower()
    if choice in ("off", "exit", "stop"):
        app.set_explore_mode(False)
        return "Left explore mode."
    if choice in ("", "on", "start"):
        app.set_explore_mode(True)
        return (
            "**Explore mode.** Read-only investigation — writes and mutating "
            "shell commands are blocked. `/explore off` to leave."
        )
    return "Usage: `/explore [on|off]`"


@command("approve", "Approve the current plan and start work", "/approve", "go")
def _approve(app: HarnessApp, args: str) -> str:
    if app.plan is None:
        return "There is no plan to approve yet."
    if not app.plan_mode:
        return "Not in plan mode — nothing is blocked."
    app.plan.approved = True
    app.set_plan_mode(False)
    return (
        f"Approved (rev {app.plan.revision}). Writes unblocked — "
        f"tell the agent to start, or it will wait."
    )


@command("classify", "Toggle automatic complexity classification", "/classify [on|off]")
def _classify(app: HarnessApp, args: str) -> str:
    choice = args.strip().lower()
    if choice in ("on", "off"):
        app.config.planning.auto_classify = choice == "on"
    state = "on" if app.config.planning.auto_classify else "off"
    return (
        f"Automatic classification: **{state}**.\n\n"
        f"When on, each request is scored 1–10 on the `{app.config.planning.classifier_role}` "
        f"model. Trivial requests are answered directly; projects go through plan "
        f"mode first. Ambiguous requests get a question with options."
    )


# --------------------------------------------------------------------------
# heartbeat
# --------------------------------------------------------------------------

HEARTBEAT_HELP = """\
**`/heartbeat <goal>`** — keep working toward a goal automatically.

The agent is nudged forward on a timer until the goal is met, so long work
does not stall waiting for you to type "continue". It also survives a dropped
connection: a failed beat backs off and retries instead of stranding the work.

Four hard caps, whichever trips first:

`--iterations N`  rounds (default 10)
`--cost N`        dollars spent inside the loop (default 1.0)
`--minutes N`     wall clock (default 30)
`--interval N`    seconds between beats (default 20)

The agent can also end it early by declaring the goal met, or by saying it
needs a human.

    /heartbeat make the whole test suite pass --iterations 20 --cost 3
    /heartbeat stop
"""

FLAG_PATTERN = re.compile(r"--(\w+)\s+([\d.]+)")


def parse_heartbeat_flags(text: str) -> tuple[str, dict[str, float]]:
    """Split a heartbeat command into its goal and its numeric flags."""
    flags = {name: float(value) for name, value in FLAG_PATTERN.findall(text)}
    goal = FLAG_PATTERN.sub("", text).strip()
    return goal, flags


@command("heartbeat", "Iterate automatically toward a goal", "/heartbeat <goal> | stop", "hb")
def _heartbeat(app: HarnessApp, args: str) -> str:
    from ..agent.heartbeat import HeartbeatLimits, StopReason

    text = args.strip()
    state = app.heartbeat.state

    if not text:
        if not app.heartbeat.active or state is None:
            return HEARTBEAT_HELP
        return (
            f"**Running** — iteration {state.iterations}/{state.limits.max_iterations}\n\n"
            f"Goal: {state.goal}\n\n"
            f"Budget left: {state.remaining(app.router.ledger.total_cost)}\n\n"
            f"`/heartbeat stop` to end it."
        )

    if text.lower() in ("stop", "off", "cancel"):
        if not app.heartbeat.active:
            return "No heartbeat is running."
        app.heartbeat.stop(StopReason.USER_STOPPED)
        return None

    goal, flags = parse_heartbeat_flags(text)
    if not goal:
        return "Give it a goal: `/heartbeat make the tests pass`"

    limits = HeartbeatLimits(
        max_iterations=int(flags.get("iterations", 10)),
        max_cost=flags.get("cost", 1.0),
        max_minutes=flags.get("minutes", 30.0),
    )
    try:
        started = app.heartbeat.start(goal, limits, flags.get("interval", 20.0))
    except (RuntimeError, ValueError) as error:
        return str(error)

    return (
        f"**Heartbeat started.** Working toward:\n\n{goal}\n\n"
        f"Caps: {started.limits.describe(app.chinese)} · "
        f"every {started.interval:.0f}s\n\n"
        f"It stops at the first cap reached, or when the agent verifies the "
        f"goal is met. `/heartbeat stop` to end it now."
    )


# --------------------------------------------------------------------------
# appearance
# --------------------------------------------------------------------------


@command("theme", "Switch the colour scheme", "/theme [name]", "colors", "colours")
def _theme(app: HarnessApp, args: str) -> str:
    if not args:
        lines = [f"Current theme: **{app.prefs.theme}**", ""]
        for spec in THEMES.values():
            marker = " ←" if spec.name == app.prefs.theme else ""
            lines.append(f"- `{spec.name}` — {spec.label}{marker}")
        lines += ["", "Switch with `/theme <name>`, or cycle with `ctrl+t`."]
        return "\n".join(lines)
    if app.set_theme_named(args.strip()):
        spec = get_theme(args.strip())
        return f"Theme set to **{spec.name}** — {spec.label}"
    return f"Unknown theme '{args.strip()}'. Available: {', '.join(theme_names())}."


@command("pet", "Show, hide or restyle the mascot", "/pet [on|off|cat|emoji]", "cat")
def _pet(app: HarnessApp, args: str) -> str:
    choice = args.strip().lower()
    if not choice:
        state = "on" if app.prefs.pet else "off"
        return (
            f"Mascot: **{state}**, style `{app.prefs.pet_style}`.\n\n"
            f"`/pet on` · `/pet off` · `/pet cat` (drawn) · `/pet emoji` (single glyph)\n\n"
            f"It shows in the sidebar (`ctrl+b`) and reflects what the agent is doing."
        )
    if choice in ("on", "show"):
        app.set_pet(enabled=True)
        return "Mascot on. Open the sidebar with `ctrl+b` to see it."
    if choice in ("off", "hide"):
        app.set_pet(enabled=False)
        return "Mascot off."
    if choice in ("cat", "emoji"):
        app.set_pet(enabled=True, style=choice)
        return f"Mascot style set to `{choice}`."
    return "Usage: `/pet on|off|cat|emoji`"


@command("markers", "Toggle the compaction dividers in the transcript", "/markers [on|off]")
def _markers(app: HarnessApp, args: str) -> str:
    choice = args.strip().lower()
    if choice in ("on", "off"):
        app.prefs.show_compaction_markers = choice == "on"
        app.prefs.save()
    state = "on" if app.prefs.show_compaction_markers else "off"
    return (
        f"Compaction markers: **{state}**. When on, a divider is drawn in the "
        f"transcript at every point where the context was condensed, showing "
        f"how many tokens it saved and holding the handoff note."
    )


# --------------------------------------------------------------------------
# skills
# --------------------------------------------------------------------------


@command("skills", "List installed skills", "/skills")
def _skills(app: HarnessApp, args: str) -> str:
    library = app.skills
    if not library.all():
        return (
            "No skills installed. Drop a folder containing `SKILL.md` into "
            "`.aiharness/skills/`, `.claude/skills/`, or the user skill directory."
        )
    lines = [f"**{len(library.all())} skill(s)**", ""]
    for skill in library.all():
        description = " ".join(skill.description.split())
        lines.append(f"- **{skill.name}** [{skill.source}] — {description[:220]}")
    if library.errors:
        lines += ["", "**Skipped**", ""] + [f"- {e}" for e in library.errors]
    return "\n".join(lines)


@command("mcp", "Show MCP servers and their tools", "/mcp [list|reconnect|tools]")
async def _mcp(app: HarnessApp, args: str) -> str:
    action = args.strip().lower() or "list"

    if action == "reconnect":
        statuses = await app.mcp.reconnect()
        app.rebuild_tools()
        lines = ["Reconnected.", ""]
        for status in statuses:
            glyph = "✓" if status.connected else "✗"
            detail = f"{status.tool_count} tool(s)" if status.connected else status.error
            lines.append(f"- {glyph} `{status.id}` — {detail}")
        return "\n".join(lines)

    if action == "tools":
        tools = app.mcp.tools
        if not tools:
            return "No MCP tools are available."
        lines = [f"**{len(tools)} MCP tool(s)**", ""]
        for tool in tools:
            flag = " (read-only)" if tool.spec.read_only else ""
            summary = " ".join(tool.spec.description.split())[:140]
            lines.append(f"- `{tool.name}`{flag} — {summary}")
        return "\n".join(lines)

    if not app.config.mcp_servers:
        return (
            "No MCP servers configured.\n\n"
            "Add them under `mcp_servers:` in your config — a local server needs "
            "a `command`, a hosted one needs a `url`. Their tools then appear to "
            "the model as `mcp__<server>__<tool>` and go through the normal "
            "permission engine."
        )

    lines = [f"**{len(app.config.mcp_servers)} MCP server(s)**", ""]
    for server in app.config.mcp_servers:
        status = app.mcp.statuses.get(server.id)
        if status is None:
            state = "not connected" if server.enabled else "disabled"
        elif status.connected:
            state = f"connected · {status.tool_count} tool(s)"
            if status.server_name:
                state += f" · {status.server_name} {status.version}".rstrip()
        else:
            state = f"failed — {status.error}"
        lines.append(f"- `{server.id}` {server.describe()}\n  {state}")
    lines += ["", "`/mcp tools` lists the tools, `/mcp reconnect` retries."]
    return "\n".join(lines)


@command("learn", "Mine past sessions for habits worth saving as skills", "/learn [save <name>]")
async def _learn(app: HarnessApp, args: str) -> str:
    from ..constants import LEARNING_MIN_OCCURRENCES
    from ..workflows.learning import collect_digests, mine_skills

    action, _, rest = args.strip().partition(" ")

    if action == "save":
        return _save_candidate(app, rest.strip())

    digests = collect_digests(app.sessions, workspace=app.workspace)
    if len(digests) < LEARNING_MIN_OCCURRENCES:
        return (
            f"Only {len(digests)} usable session(s) here. A habit needs to show up in "
            f"at least {LEARNING_MIN_OCCURRENCES} before it is worth writing down — "
            f"come back after more work."
        )

    binding = app.config.role("main")
    if binding is None:
        return "No main role configured."

    app._notice(f"reading {len(digests)} session(s)…")
    candidates = await mine_skills(
        digests, app.router, Selection.from_binding(binding)
    )
    app._learning_candidates = {c.name: c for c in candidates}

    if not candidates:
        return (
            f"Read {len(digests)} session(s) and found nothing that clears the bar. "
            f"That is a normal result — a pattern has to recur across sessions and "
            f"not already be obvious from the code."
        )

    lines = [f"**{len(candidates)} candidate skill(s)** from {len(digests)} session(s)", ""]
    for candidate in candidates:
        lines.append(f"- {candidate.summary()}")
        if candidate.evidence:
            lines.append(f"  evidence: {'; '.join(candidate.evidence[:3])}")
    lines += [
        "",
        "Nothing has been written. Review one with `/learn save <name>` to install it "
        "into `.aiharness/skills/`, then `/reload`.",
    ]
    return "\n".join(lines)


def _save_candidate(app: HarnessApp, name: str) -> str:
    from ..workflows.learning import save_candidate

    candidates = getattr(app, "_learning_candidates", {})
    if not candidates:
        return "Run `/learn` first."
    candidate = candidates.get(name)
    if candidate is None:
        return f"No candidate '{name}'. Available: {', '.join(candidates)}"
    try:
        path = save_candidate(candidate, app.workspace / ".aiharness" / "skills")
    except FileExistsError as error:
        return f"{error}. Delete or rename the existing skill first."
    except OSError as error:
        return f"Could not write the skill: {error}"
    app.reload_skills()
    return f"Wrote `{path}` and reloaded. {len(app.skills.all())} skill(s) available."


@command("reload", "Re-scan skills and reload configuration", "/reload")
def _reload(app: HarnessApp, args: str) -> str:
    app.reload_skills()
    app.agent.invalidate_system_prompt()
    return f"Reloaded {len(app.skills.all())} skill(s)."


# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------


@command("new", "Start a fresh session", "/new")
def _new(app: HarnessApp, args: str) -> str:
    app.start_new_session()
    return "Started a new session."


@command("sessions", "List saved sessions", "/sessions", "ls")
def _sessions(app: HarnessApp, args: str) -> str:
    entries = app.sessions.list(workspace=app.workspace)
    if not entries:
        return "No saved sessions for this workspace."
    lines = [f"**{len(entries)} session(s)**", ""]
    for meta in entries:
        marker = " ←" if meta.id == app.session.meta.id else ""
        lines.append(
            f"- `{meta.id}`{marker} {meta.title or '(untitled)'}\n"
            f"  {meta.updated_label} · {meta.message_count} message(s) · ${meta.total_cost:.4f}"
        )
    lines += ["", "Resume with `/resume <id>`, delete with `/delete <id>`."]
    return "\n".join(lines)


@command("resume", "Load a saved session", "/resume <id>")
def _resume(app: HarnessApp, args: str) -> str:
    if not args:
        return "Usage: `/resume <id>` — see `/sessions`."
    if app.resume_session(args.strip()):
        return f"Resumed `{args.strip()}`."
    return f"No session `{args.strip()}` found."


@command("clear", "Erase this conversation, keeping the session", "/clear")
async def _clear(app: HarnessApp, args: str) -> str | None:
    confirmed = await app.confirm(
        "Erase this conversation?",
        "The messages are deleted from disk. The session itself is kept.",
    )
    if not confirmed:
        return "Cancelled."
    app.clear_conversation()
    return None


@command("delete", "Delete a session, or every session", "/delete [id|all]")
async def _delete(app: HarnessApp, args: str) -> str | None:
    target = args.strip()
    if target == "all":
        entries = app.sessions.list(workspace=app.workspace)
        confirmed = await app.confirm(
            f"Delete all {len(entries)} session(s) for this workspace?",
            "Every message, summary and cost record is removed from disk. "
            "This cannot be undone.",
        )
        if not confirmed:
            return "Cancelled."
        removed = app.sessions.delete_all(workspace=app.workspace)
        app.start_new_session()
        return f"Deleted {removed} session(s)."

    session_id = target or app.session.meta.id
    confirmed = await app.confirm(
        f"Delete session {session_id}?", "This cannot be undone."
    )
    if not confirmed:
        return "Cancelled."
    if not app.sessions.delete(session_id):
        return f"No session `{session_id}` found."
    if session_id == app.session.meta.id:
        app.start_new_session()
    return f"Deleted `{session_id}`."


@command("history", "Show the full stored transcript, including compacted turns", "/history")
def _history(app: HarnessApp, args: str) -> str:
    messages = app.session.full_history
    compactions = app.session.compactions
    lines = [
        f"**{len(messages)} stored message(s)**, "
        f"{len(compactions)} compaction(s), "
        f"{len(app.agent.messages)} currently in context.",
        "",
    ]
    for record in compactions:
        lines.append(
            f"- compacted messages 0–{record.replaced_through} at "
            f"{datetime.fromtimestamp(record.at):%Y-%m-%d %H:%M} "
            f"({record.tokens_before:,} → {record.tokens_after:,} tokens)"
        )
    if compactions:
        lines += ["", "`/uncompact` restores the full transcript to the context."]
    return "\n".join(lines)


@command("compact", "Compact the context now", "/compact")
async def _compact(app: HarnessApp, args: str) -> str | None:
    event = await app.agent.compact_now()
    if event is None:
        return "Nothing to compact yet."
    app._handle_compacted(event)  # noqa: SLF001 - the app owns this rendering
    return None


@command("uncompact", "Restore the full transcript into the context", "/uncompact")
def _uncompact(app: HarnessApp, args: str) -> str:
    dropped = app.agent.restore_full_history()
    if not dropped:
        return "Nothing was compacted."
    return (
        f"Restored the full transcript ({dropped} compaction(s) undone, "
        f"{app.agent.context_used():,} tokens in context)."
    )


@command("cost", "Show token usage and spend", "/cost")
def _cost(app: HarnessApp, args: str) -> str:
    ledger = app.router.ledger
    usage = ledger.total_usage
    cache = app.agent.state.cache
    lines = [
        "**Session usage**",
        "",
        f"- input {usage.input_tokens:,} · output {usage.output_tokens:,} "
        f"· cached {usage.cached_tokens:,}",
        f"- prompt cache hit rate: {cache.hit_rate * 100:.1f}%",
        f"- total: **${ledger.total_cost:.4f}** over {len(ledger.records)} call(s)",
        "",
        "**By model and account**",
        "",
    ]
    for key, (model_usage, cost, count) in sorted(ledger.by_model().items()):
        lines.append(
            f"- `{key}` — {count} call(s), {model_usage.total:,} tokens, ${cost:.4f}"
        )
    by_role = ledger.by_role()
    if len(by_role) > 1:
        lines += ["", "**By role**", ""]
        for role, cost in sorted(by_role.items(), key=lambda kv: -kv[1]):
            lines.append(f"- {role}: ${cost:.4f}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# scheduled jobs
# --------------------------------------------------------------------------

SCHEDULE_HELP = """\
**Schedule syntax**

- `daily 09:00` or `daily 09:00,18:30`
- `weekly mon,thu 09:30` (also 周一、周四)
- `every 30m` / `every 4h`
- `cron */15 9-18 * * 1-5`
- `once 2026-08-05 09:00`
"""

INTERVAL_RE = re.compile(r"^(\d+)\s*([mh])$", re.IGNORECASE)


def parse_schedule(text: str) -> Schedule:
    """Parse the schedule mini-language used by ``/job add``.

    Args:
      text: One of the forms documented in :data:`SCHEDULE_HELP`.

    Returns:
      The parsed :class:`~aiharness.scheduler.cron.Schedule`.

    Raises:
      ScheduleError: If the text is not a recognised schedule.
    """
    parts = text.strip().split()
    if not parts:
        raise ScheduleError("empty schedule")
    kind = parts[0].lower()

    if kind == "daily":
        times = parts[1].split(",") if len(parts) > 1 else ["09:00"]
        return Schedule.daily(*times)

    if kind == "weekly":
        if len(parts) < SCHEDULE_MIN_TOKENS:
            raise ScheduleError("weekly needs weekdays, e.g. `weekly mon,thu 09:30`")
        weekdays = [parse_weekday(token) for token in parts[1].split(",")]
        times = parts[2].split(",") if len(parts) > SCHEDULE_MIN_TOKENS else ["09:00"]
        return Schedule.weekly(weekdays, *times)

    if kind == "every":
        if len(parts) < SCHEDULE_MIN_TOKENS:
            raise ScheduleError("every needs an interval, e.g. `every 30m`")
        match = INTERVAL_RE.match(parts[1])
        if not match:
            raise ScheduleError(f"interval must look like 30m or 4h, got {parts[1]!r}")
        value = int(match.group(1))
        minutes = value * MINUTES_PER_HOUR if match.group(2).lower() == "h" else value
        return Schedule.every(minutes)

    if kind == "cron":
        return Schedule.from_cron(" ".join(parts[1:]))

    if kind == "once":
        stamp = " ".join(parts[1:])
        try:
            moment = datetime.strptime(stamp, "%Y-%m-%d %H:%M")
        except ValueError as error:
            raise ScheduleError(f"once needs `YYYY-MM-DD HH:MM`, got {stamp!r}") from error
        return Schedule.once_at(moment)

    raise ScheduleError(f"unknown schedule kind {kind!r}")


@command(
    "job",
    "Manage scheduled tasks",
    "/job add <name> | <schedule> | <prompt>   ·   /job list|rm|on|off|run|show <id>",
    "jobs",
)
async def _job(app: HarnessApp, args: str) -> str:
    if not args or args.strip() == "list":
        return _render_jobs(app)

    action, _, rest = args.partition(" ")
    action = action.lower()
    rest = rest.strip()

    if action == "add":
        return _job_add(app, rest)
    if action == "help":
        return SCHEDULE_HELP
    if not rest:
        return f"Usage: `/job {action} <id>`"

    if action in ("rm", "remove", "delete"):
        confirmed = await app.confirm(f"Delete scheduled job {rest}?")
        if not confirmed:
            return "Cancelled."
        return f"Deleted job `{rest}`." if app.jobs.remove(rest) else f"No job `{rest}`."
    if action in ("on", "enable"):
        job = app.jobs.set_enabled(rest, True)
        return f"Enabled `{job.name}`, next run {job.next_run_label()}." if job else f"No job `{rest}`."
    if action in ("off", "disable"):
        job = app.jobs.set_enabled(rest, False)
        return f"Disabled `{job.name}`." if job else f"No job `{rest}`."
    if action == "run":
        job = app.jobs.get(rest)
        if job is None:
            return f"No job `{rest}`."
        app.run_job_now(job.id)
        return f"Running `{job.name}` now — results will appear here when it finishes."
    if action == "show":
        return _job_show(app, rest)
    return f"Unknown action `{action}`. Try `/job help`."


def _job_add(app: HarnessApp, rest: str) -> str:
    parts = [piece.strip() for piece in rest.split("|")]
    if len(parts) < JOB_ADD_FIELD_COUNT:
        return (
            "Usage: `/job add <name> | <schedule> | <prompt>`\n\n"
            "Example: `/job add deps | weekly mon 09:00 | Check for outdated "
            "dependencies and open a summary in NOTES.md`\n\n" + SCHEDULE_HELP
        )
    name, schedule_text, prompt = parts[0], parts[1], " | ".join(parts[2:])
    try:
        schedule = parse_schedule(schedule_text)
    except ScheduleError as error:
        return f"{error}\n\n{SCHEDULE_HELP}"

    job = app.jobs.add(
        name=name,
        prompt=prompt,
        schedule=schedule,
        workspace=app.workspace,
        model=app.agent.selection.model_id,
        permission_mode="auto" if app.permissions.mode == "ask" else app.permissions.mode,
    )
    app.ensure_scheduler()
    note = ""
    if app.permissions.mode == "ask":
        note = (
            "\n\nScheduled runs cannot answer approval prompts, so this job runs "
            "in `auto` mode. Change it in the jobs file if you need `yolo`."
        )
    return (
        f"Added **{job.name}** (`{job.id}`) — {job.describe(app.chinese)}, "
        f"next run {job.next_run_label()}.{note}"
    )


def _job_show(app: HarnessApp, job_id: str) -> str:
    job = app.jobs.get(job_id)
    if job is None:
        return f"No job `{job_id}`."
    lines = [
        f"**{job.name}** (`{job.id}`)",
        "",
        f"- schedule: {job.describe(app.chinese)}",
        f"- next run: {job.next_run_label()}",
        f"- model: {job.model or 'main role'} · mode: {job.permission_mode} · "
        f"max turns: {job.max_turns}",
        f"- workspace: {job.workspace}",
        "",
        "**Prompt**",
        "",
        job.prompt,
    ]
    if job.history:
        lines += ["", "**Recent runs**", ""]
        for record in reversed(job.history[-5:]):
            glyph = "✓" if record.ok else "✗"
            detail = record.error or (record.summary or "")[:120]
            lines.append(
                f"- {glyph} {record.started_label} ({record.duration:.0f}s, "
                f"${record.cost:.4f}) {detail}"
            )
    return "\n".join(lines)


def _render_jobs(app: HarnessApp) -> str:
    jobs = app.jobs.all()
    if not jobs:
        return (
            "No scheduled jobs.\n\n"
            "Add one with `/job add <name> | <schedule> | <prompt>`.\n\n" + SCHEDULE_HELP
        )
    lines = [f"**{len(jobs)} scheduled job(s)**", ""]
    for job in jobs:
        state = "on" if job.enabled else "off"
        last = job.last_record
        outcome = ""
        if last:
            outcome = f" · last {'ok' if last.ok else 'failed'} {last.started_label}"
        lines.append(
            f"- `{job.id}` **{job.name}** [{state}] — {job.describe(app.chinese)} "
            f"· next {job.next_run_label()}{outcome}"
        )
    lines += ["", "`/job show <id>` for detail, `/job run <id>` to run immediately."]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# exit
# --------------------------------------------------------------------------


@command("quit", "Exit", "/quit", "exit", "q")
def _quit(app: HarnessApp, args: str) -> None:
    app.exit()
