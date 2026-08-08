"""Load, merge and persist configuration.

Search order (later wins on a per-key basis):
    1. bundled defaults
    2. ~/.aiharness/config.yaml       (user)
    3. <workspace>/.aiharness.yaml    (project)
    4. AIH_CONFIG env var             (explicit override)

``${VAR}`` and ``${VAR:-fallback}`` are expanded from the environment
anywhere a string appears, so API keys never have to be written to disk.
"""

from __future__ import annotations

import os
import re
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml
from platformdirs import user_config_dir

from .schema import (
    AdversarialConfig,
    BrowserConfig,
    Config,
    ContextConfig,
    DelegationConfig,
    DesktopConfig,
    EffortSpec,
    MarketConfig,
    MCPServerConfig,
    ModelDef,
    PermissionConfig,
    PlanningConfig,
    Pricing,
    ProviderAccount,
    ResearchConfig,
    RoleBinding,
    UIConfig,
    VerifyConfig,
    WorkflowConfig,
)

ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def default_config_path() -> Path:
    """Where the config is read from *and* written to.

    ``AIH_CONFIG`` overrides both. It used to add a read-only layer, which
    meant edits made in the UI were saved to the platform path while the
    override kept being read — the settings silently split across two files
    and changes appeared not to stick.
    """
    override = os.environ.get("AIH_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path(user_config_dir("aiharness", appauthor=False)) / "config.yaml"


def project_config_path(workspace: Path) -> Path:
    return workspace / ".aiharness.yaml"


# --------------------------------------------------------------------------
# env expansion + deep merge
# --------------------------------------------------------------------------


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        def sub(m: re.Match[str]) -> str:
            var, fallback = m.group(1), m.group(2)
            return os.environ.get(var, fallback if fallback is not None else "")

        return ENV_PATTERN.sub(sub, value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge dicts. Lists of dicts with an 'id' merge by id; other lists replace."""
    out = dict(base)
    for key, val in overlay.items():
        prev = out.get(key)
        if isinstance(prev, dict) and isinstance(val, dict):
            out[key] = _merge(prev, val)
        elif (
            isinstance(prev, list)
            and isinstance(val, list)
            and all(isinstance(x, dict) and "id" in x for x in prev + val)
        ):
            by_id = {x["id"]: dict(x) for x in prev}
            for item in val:
                if item["id"] in by_id:
                    by_id[item["id"]] = _merge(by_id[item["id"]], item)
                else:
                    by_id[item["id"]] = dict(item)
            out[key] = list(by_id.values())
        else:
            out[key] = val
    return out


# --------------------------------------------------------------------------
# dict -> dataclass
# --------------------------------------------------------------------------


def _build(cls: type, data: Any) -> Any:
    """Instantiate a dataclass tree from plain dicts, ignoring unknown keys."""
    if data is None:
        return cls()
    if not isinstance(data, dict):
        return data

    kwargs: dict[str, Any] = {}
    known = {f.name: f for f in fields(cls)}
    for name, raw in data.items():
        f = known.get(name)
        if f is None:
            continue
        ftype = f.type
        # Resolve string annotations from `from __future__ import annotations`.
        if isinstance(ftype, str):
            ftype = _resolve(ftype)
        kwargs[name] = _coerce(ftype, raw)
    return cls(**kwargs)


_TYPE_TABLE = {
    "ProviderAccount": ProviderAccount,
    "MCPServerConfig": MCPServerConfig,
    "ModelDef": ModelDef,
    "EffortSpec": EffortSpec,
    "Pricing": Pricing,
    "RoleBinding": RoleBinding,
    "PermissionConfig": PermissionConfig,
    "WorkflowConfig": WorkflowConfig,
    "AdversarialConfig": AdversarialConfig,
    "VerifyConfig": VerifyConfig,
    "ResearchConfig": ResearchConfig,
    "DelegationConfig": DelegationConfig,
    "ContextConfig": ContextConfig,
    "BrowserConfig": BrowserConfig,
    "DesktopConfig": DesktopConfig,
    "PlanningConfig": PlanningConfig,
    "MarketConfig": MarketConfig,
    "UIConfig": UIConfig,
}


def _resolve(annotation: str) -> Any:
    """Best-effort resolution of the handful of annotation shapes we use."""
    a = annotation.strip()
    for name, cls in _TYPE_TABLE.items():
        if a == name:
            return cls
        if a == f"list[{name}]":
            return [cls]
        if a == f"dict[str, {name}]":
            return {"__map__": cls}
    return None


def _coerce(ftype: Any, raw: Any) -> Any:
    if ftype is None:
        return raw
    if isinstance(ftype, list) and ftype and is_dataclass(ftype[0]):
        return [_build(ftype[0], item) for item in (raw or [])]
    if isinstance(ftype, dict) and "__map__" in ftype:
        cls = ftype["__map__"]
        return {k: _build(cls, v) for k, v in (raw or {}).items()}
    if is_dataclass(ftype) and isinstance(raw, dict):
        return _build(ftype, raw)
    return raw


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {path}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


class ConfigError(Exception):
    pass


def load_config(workspace: Path | None = None, explicit: Path | None = None) -> Config:
    workspace = workspace or Path.cwd()
    layers: list[dict[str, Any]] = []

    layers.append(_read_yaml(default_config_path()))
    layers.append(_read_yaml(project_config_path(workspace)))

    if explicit:
        layers.append(_read_yaml(Path(explicit)))

    merged: dict[str, Any] = {}
    for layer in layers:
        merged = _merge(merged, layer)

    merged = _expand(merged)

    cfg = _build_config(merged)
    _attach_stored_keys(cfg)
    return cfg


def _attach_stored_keys(cfg: Config) -> None:
    """Fill in keys that were pasted rather than exported.

    An account whose key came from the clipboard has nothing in the config
    file — the secret is in the credential store, keyed by account id.
    """
    from ..credentials import CredentialStore

    store = CredentialStore()
    for account in cfg.accounts:
        if account.api_key or account.api_key_env:
            continue
        stored = store.get(account.id)
        if stored:
            account.api_key = stored


def _build_config(merged: dict[str, Any]) -> Config:
    return Config(
        accounts=[_build(ProviderAccount, a) for a in merged.get("accounts", [])],
        models=[_build(ModelDef, m) for m in merged.get("models", [])],
        mcp_servers=[
            _build(MCPServerConfig, s) for s in merged.get("mcp_servers", [])
        ],
        roles={k: _build(RoleBinding, v) for k, v in (merged.get("roles") or {}).items()},
        permissions=_build(PermissionConfig, merged.get("permissions")),
        workflows=_build(WorkflowConfig, merged.get("workflows")),
        context=_build(ContextConfig, merged.get("context")),
        ui=_build(UIConfig, merged.get("ui")),
        desktop=_build(DesktopConfig, merged.get("desktop")),
        browser=_build(BrowserConfig, merged.get("browser")),
        planning=_build(PlanningConfig, merged.get("planning")),
        market=_build(MarketConfig, merged.get("market")),
        route_strategy=merged.get("route_strategy", "priority"),
        skill_paths=merged.get("skill_paths", []),
        system_prompt_append=merged.get("system_prompt_append", ""),
        max_agent_turns=merged.get("max_agent_turns", 100),
    )


def save_config(cfg: Config, path: Path | None = None) -> Path:
    """Write the configuration, with secrets replaced by their env references.

    Args:
      cfg: The live configuration. It is not modified.
      path: Destination; defaults to the user config file.

    Returns:
      The path written.
    """
    path = path or default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    import copy

    storable = copy.copy(cfg)
    storable.accounts = [account.for_storage() for account in cfg.accounts]
    path.write_text(
        yaml.safe_dump(storable.to_dict(), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


EXAMPLE = """\
# Dr.Wang configuration
# Env vars are expanded: ${VAR} or ${VAR:-fallback}. Keep keys out of this file.

route_strategy: priority        # priority | round_robin | least_used | random

# ---------------------------------------------------------------------------
# API accounts and models.
#
# Nothing is configured out of the box, and the harness never guesses at your
# environment: no key is read unless you name it here, and no model is offered
# unless the account it belongs to actually lists it.
#
# The quickest way to fill this in is from inside the app:
#
#     /setup                       walk through it
#     /accounts add ds https://api.deepseek.com/v1 DEEPSEEK_API_KEY
#     /models add ds               pick from what that account really serves
#     /role main <model>           assign the roles
#
# Or write it by hand. Two accounts may share a base_url and differ only in
# api_key — that is how you attach several accounts of one vendor.
# ---------------------------------------------------------------------------
accounts: []
  # - id: ds-a
  #   base_url: https://api.deepseek.com/v1
  #   api_key: ${DEEPSEEK_API_KEY}      # env var, never the key itself
  #   priority: 10
  # - id: ds-b                          # same vendor, second account
  #   base_url: https://api.deepseek.com/v1
  #   api_key: ${DEEPSEEK_API_KEY_2}
  #   priority: 20

models: []
  # - id: reasoner                      # what you type in /model
  #   model: deepseek-reasoner          # what the API is sent
  #   accounts: [ds-a, ds-b]            # every account that can serve it
  #   context_windows: [32768, 65536, 131072]
  #   default_context: 65536
  #   max_output_tokens: 16384
  #   supports_temperature: false
  #   default_effort: high
  #   effort:
  #     mode: reasoning_effort          # or thinking_budget / temperature / none
  #     levels: {low: low, medium: medium, high: high}
  #   pricing: {input: 0.55, output: 2.19}   # USD per 1M, for the cost readout

# ---------------------------------------------------------------------------
# Roles decide which model does which job. Only 'main' is required; every
# other role falls back to it. A role pointing at a model you have not
# configured is rejected rather than silently ignored.
# ---------------------------------------------------------------------------
roles: {}
  # main:       {model: reasoner, effort: high}
  # fast:       {model: chat}
  # cheap:      {model: mini}           # bulk work, summaries, compaction
  # verifier:   {model: chat}
  # adversary:  {model: other-vendor}   # a different family sees other bugs
  # researcher: {model: chat}
  # compactor:  {model: mini}


permissions:
  mode: ask                 # ask | auto | yolo
  block_catastrophic: true  # keep true even in yolo
  allow:
    - "Read(*)"
    - "Glob(*)"
    - "Grep(*)"
    - "Bash(git status)"
    - "Bash(git diff:*)"
    - "Bash(git log:*)"
    - "Bash(ls:*)"
  ask: []                   # always prompt, even in auto/yolo (e.g. "Bash(npm publish:*)")
  deny:
    - "Bash(curl:*)"
    - "Bash(sudo:*)"
  additional_directories: []
  # prompt_timeout: 0       # seconds; 0 = wait (GUI has a long safety ceiling)

workflows:
  adversarial:
    enabled: true
    rounds: 2
    adversary_role: adversary
    stop_when_clean: true
    auto_trigger: false
  verify:
    enabled: true
    verifier_role: verifier
    auto_trigger: false
    max_fix_attempts: 2
    commands: []            # e.g. ["pytest -q", "ruff check ."]
  research:
    parallel: 3
    synthesis_role: main
    max_turns: 12
    models:
      - {model: ds-chat}
      - {model: kimi}
      - {model: qwen-turbo}
  delegation:
    enabled: true
    cheap_role: cheap
    fast_role: fast
    expose_delegate_tool: true

context:
  compact_threshold: 0.82
  keep_recent_messages: 8
  max_tool_result_chars: 12000
  bash_success_chars: 2500
  bash_error_chars: 6000
  read_result_chars: 12000
  auto_compact: true

ui:
  show_reasoning: true
  show_cost: true
  stream: true
  auto_apply_edits: false   # yolo 模式下 Write/Edit 是否跳过审阅条

# ---------------------------------------------------------------------------
# MCP servers. Their tools appear to the model as mcp__<id>__<tool> and go
# through the same permission engine as everything else.
# Local server -> command; hosted server -> url.
# ---------------------------------------------------------------------------
mcp_servers: []
  # - id: filesystem
  #   command: npx
  #   args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
  #
  # - id: github
  #   command: npx
  #   args: ["-y", "@modelcontextprotocol/server-github"]
  #   env:
  #     GITHUB_PERSONAL_ACCESS_TOKEN: ${GITHUB_TOKEN}
  #   tools_deny: [delete_repository]
  #
  # - id: hosted
  #   url: https://example.com/mcp
  #   headers:
  #     Authorization: Bearer ${SOME_MCP_TOKEN}

# ---------------------------------------------------------------------------
# Extra directories scanned for SKILL.md files.
#
# These are searched on top of the built-in locations, which already include
# <workspace>/.aiharness/skills, <workspace>/.claude/skills, <workspace>/skills
# and ~/.claude/skills — so an existing Claude Code skill library works with no
# changes at all. Point this at a shared library to reuse it across projects.
# ---------------------------------------------------------------------------
skill_paths: []
  # - C:\\ClaudeProjects\\skills

max_agent_turns: 100
"""


def write_example_config(path: Path | None = None, force: bool = False) -> Path:
    path = path or default_config_path()
    if path.exists() and not force:
        raise ConfigError(f"{path} already exists (use --force to overwrite)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(EXAMPLE, encoding="utf-8")
    return path
