"""Durable Claude Code profiles for the GUI Claude panel.

Only auth / endpoint / model selection lives here. Claude Code itself still
owns the agent loop; we just set env/flags before spawning the native CLI.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from platformdirs import user_config_dir

from ..config.schema import ProviderAccount
from ..credentials import CredentialStore
from ..providers import proxy as proxy_mod
from .profile_bridge import (
    account_secret,
    claude_base_url_for_account,
    claude_model_for_account,
    detect_claude_template,
    is_kimi_coding,
    match_agent_secret,
    profile_id_for_account,
)

PROFILES_FILE = "claude_profiles.json"
CRED_PREFIX = "claude:"
ID_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,47}$")


def ensure_claude_third_party_onboarding(config_dir: Path) -> None:
    """Seed flags Kimi/DeepSeek docs require so Claude Code skips Anthropic login."""
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / ".claude.json"
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            data = {}
    changed = False
    for key, value in (
        ("hasCompletedOnboarding", True),
        ("penguinModeOrgEnabled", True),
    ):
        if data.get(key) is not True:
            data[key] = value
            changed = True
    if changed or not path.is_file():
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

TEMPLATES: dict[str, dict[str, str]] = {
    "anthropic": {
        "name": "Anthropic · API Key",
        "base_url": "",
        "model": "",
        "env_key": "ANTHROPIC_API_KEY",
        "auth_mode": "api_key",
    },
    "login": {
        "name": "Anthropic · 订阅登录",
        "base_url": "",
        "model": "",
        "env_key": "",
        "auth_mode": "login",
    },
    "kimi": {
        "name": "Kimi Coding · Anthropic",
        # Trailing slash matters: urljoin-style clients must keep the /coding/ segment.
        "base_url": "https://api.kimi.com/coding/",
        # Live Coding API rejects literal ``k3[1m]`` (auth error). Use ``k3`` and
        # set CLAUDE_CODE_MAX_CONTEXT_TOKENS for 1M context instead.
        "model": "k3",
        "env_key": "ANTHROPIC_API_KEY",
        "auth_mode": "api_key",
    },
    "kimi-platform": {
        "name": "Kimi Platform · Moonshot Anthropic",
        "base_url": "https://api.moonshot.cn/anthropic",
        "model": "kimi-k3[1m]",
        "env_key": "ANTHROPIC_API_KEY",
        "auth_mode": "api_key",
    },
    "deepseek": {
        "name": "DeepSeek · Anthropic",
        "base_url": "https://api.deepseek.com/anthropic",
        "model": "deepseek-v4-flash",
        "env_key": "ANTHROPIC_API_KEY",
        "auth_mode": "api_key",
    },
    "glm": {
        "name": "GLM · Anthropic 兼容",
        "base_url": "",
        "model": "glm-4.5",
        "env_key": "ANTHROPIC_API_KEY",
        "auth_mode": "api_key",
    },
    "custom": {
        "name": "Custom / Anthropic 兼容代理",
        "base_url": "https://api.anthropic.com",
        "model": "",
        "env_key": "ANTHROPIC_API_KEY",
        "auth_mode": "api_key",
    },
}

#: Known models for the Claude panel picker (per template).
KNOWN_CLAUDE_MODELS: dict[str, list[dict[str, Any]]] = {
    "kimi": [
        {
            "id": "k3",
            "label": "k3 (1M via env)",
            "efforts": ["low", "medium", "high", "max"],
            "default_effort": "high",
        },
        {
            "id": "k3-256k",
            "label": "k3-256k",
            "efforts": ["low", "medium", "high", "max"],
            "default_effort": "high",
        },
        {
            "id": "kimi-for-coding",
            "label": "kimi-for-coding",
            "efforts": ["low", "medium", "high"],
            "default_effort": "high",
        },
        {
            "id": "kimi-for-coding-highspeed",
            "label": "kimi-for-coding-highspeed",
            "efforts": ["low", "medium", "high"],
            "default_effort": "medium",
        },
    ],
    "kimi-platform": [
        {
            "id": "kimi-k3[1m]",
            "label": "kimi-k3[1m]",
            "efforts": ["low", "medium", "high", "max"],
            "default_effort": "high",
        },
        {
            "id": "kimi-k2.5",
            "label": "kimi-k2.5",
            "efforts": ["low", "medium", "high"],
            "default_effort": "high",
        },
    ],
    "deepseek": [
        {
            "id": "deepseek-v4-flash",
            "label": "deepseek-v4-flash",
            "efforts": ["low", "medium", "high"],
            "default_effort": "medium",
        },
        {
            "id": "deepseek-v4-pro[1m]",
            "label": "deepseek-v4-pro[1m]",
            "efforts": ["low", "medium", "high"],
            "default_effort": "high",
        },
    ],
    "anthropic": [
        {
            "id": "claude-opus-4-5",
            "label": "claude-opus-4-5",
            "efforts": ["low", "medium", "high", "max"],
            "default_effort": "high",
        },
        {
            "id": "claude-sonnet-4-5",
            "label": "claude-sonnet-4-5",
            "efforts": ["low", "medium", "high"],
            "default_effort": "high",
        },
        {
            "id": "claude-haiku-4-5",
            "label": "claude-haiku-4-5",
            "efforts": ["low", "medium", "high"],
            "default_effort": "medium",
        },
    ],
    "login": [
        {
            "id": "claude-opus-4-5",
            "label": "claude-opus-4-5",
            "efforts": ["low", "medium", "high", "max"],
            "default_effort": "high",
        },
        {
            "id": "claude-sonnet-4-5",
            "label": "claude-sonnet-4-5",
            "efforts": ["low", "medium", "high"],
            "default_effort": "high",
        },
    ],
}


@dataclass
class ClaudeProfile:
    id: str
    name: str
    env_key: str = "ANTHROPIC_API_KEY"
    base_url: str = ""
    model: str = ""
    template: str = "anthropic"
    auth_mode: str = "api_key"  # api_key | login
    #: "" = inherit system · "direct" = no proxy · else proxy URL (e.g. Clash).
    proxy: str = ""
    note: str = ""
    has_secret: bool = False

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data["proxy_label"] = proxy_mod.describe_setting(self.proxy)
        return data


@dataclass
class ClaudeProfileStore:
    path: Path | None = None
    credentials: CredentialStore | None = None
    active_id: str = ""
    profiles: list[ClaudeProfile] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.path is None:
            override = os.environ.get("AIH_CLAUDE_PROFILES_FILE")
            self.path = (
                Path(override).expanduser()
                if override
                else Path(user_config_dir("aiharness", appauthor=False)) / PROFILES_FILE
            )
        if self.credentials is None:
            self.credentials = CredentialStore()
        self.load()

    def load(self) -> None:
        assert self.path is not None and self.credentials is not None
        if not self.path.is_file():
            self.profiles = []
            self.active_id = ""
            self.ensure_defaults()
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.profiles = []
            self.active_id = ""
            self.ensure_defaults()
            return
        raw = payload.get("profiles") if isinstance(payload, dict) else None
        profiles: list[ClaudeProfile] = []
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                auth_mode = str(item.get("auth_mode") or "").strip()
                template = str(item.get("template") or "anthropic")
                if not auth_mode:
                    auth_mode = "login" if template == "login" else "api_key"
                try:
                    proxy_value = proxy_mod.normalise(str(item.get("proxy") or ""))
                except proxy_mod.ProxyError:
                    proxy_value = ""
                profiles.append(
                    ClaudeProfile(
                        id=str(item["id"]),
                        name=str(item.get("name") or item["id"]),
                        env_key=str(item.get("env_key") or ("ANTHROPIC_API_KEY" if auth_mode != "login" else "")),
                        base_url=str(item.get("base_url") or ""),
                        model=str(item.get("model") or ""),
                        template=template,
                        auth_mode=auth_mode if auth_mode in {"api_key", "login"} else "api_key",
                        proxy=proxy_value,
                        note=str(item.get("note") or ""),
                    )
                )
        self.profiles = profiles
        self.active_id = str(payload.get("active_id") or "")
        self.ensure_defaults()
        self._refresh_secret_flags()
        self.repair_profiles()

    def save(self) -> Path:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "active_id": self.active_id,
            "profiles": [
                {
                    "id": p.id,
                    "name": p.name,
                    "env_key": p.env_key,
                    "base_url": p.base_url,
                    "model": p.model,
                    "template": p.template,
                    "auth_mode": p.auth_mode,
                    "proxy": p.proxy,
                    "note": p.note,
                }
                for p in self.profiles
            ],
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.path

    def ensure_defaults(self) -> None:
        changed = False
        if not self.get("anthropic"):
            tpl = TEMPLATES["anthropic"]
            self.profiles.append(
                ClaudeProfile(
                    id="anthropic",
                    name=tpl["name"],
                    env_key=tpl["env_key"],
                    base_url=tpl["base_url"],
                    model=tpl["model"],
                    template="anthropic",
                    auth_mode="api_key",
                )
            )
            changed = True
        if not self.get("login"):
            tpl = TEMPLATES["login"]
            self.profiles.append(
                ClaudeProfile(
                    id="login",
                    name=tpl["name"],
                    env_key="",
                    base_url="",
                    model="",
                    template="login",
                    auth_mode="login",
                    note="使用 claude auth login 订阅登录",
                )
            )
            changed = True
        if not self.active_id or self.get(self.active_id) is None:
            self.active_id = "anthropic" if self.get("anthropic") else self.profiles[0].id
            changed = True
        if changed:
            self.save()

    def repair_profiles(self) -> int:
        """Fix known-bad DeepSeek / Kimi Claude profile URLs and models."""
        fixed = 0
        for profile in self.profiles:
            before = (profile.base_url, profile.model, profile.template)
            repaired = _repair_one_profile(profile)
            if repaired and (profile.base_url, profile.model, profile.template) != before:
                fixed += 1
        if fixed:
            self.save()
        return fixed

    def _refresh_secret_flags(self) -> None:
        assert self.credentials is not None
        for profile in self.profiles:
            profile.has_secret = bool(self.credentials.get(CRED_PREFIX + profile.id))

    def get(self, profile_id: str) -> ClaudeProfile | None:
        return next((p for p in self.profiles if p.id == profile_id), None)

    def list_public(self) -> list[dict[str, Any]]:
        self._refresh_secret_flags()
        return [p.public() for p in self.profiles]

    def templates_public(self) -> list[dict[str, str]]:
        return [{"id": key, **value} for key, value in TEMPLATES.items()]

    def models_for(self, profile: ClaudeProfile) -> list[dict[str, Any]]:
        models = list(KNOWN_CLAUDE_MODELS.get(profile.template, []))
        if profile.model and profile.model not in {m["id"] for m in models}:
            models.insert(
                0,
                {
                    "id": profile.model,
                    "label": profile.model,
                    "efforts": _default_efforts_for(profile.template, profile.model),
                    "default_effort": "",
                },
            )
        return models

    def upsert(
        self,
        *,
        profile_id: str,
        name: str,
        env_key: str = "ANTHROPIC_API_KEY",
        base_url: str = "",
        model: str = "",
        template: str = "anthropic",
        auth_mode: str = "",
        api_key: str = "",
        proxy: str = "",
        note: str = "",
        make_active: bool = False,
    ) -> ClaudeProfile:
        assert self.credentials is not None
        profile_id = profile_id.strip()
        if not ID_RE.match(profile_id):
            raise ValueError("profile id must be letters/digits/_/- and start with a letter")
        tpl = TEMPLATES.get(template) or TEMPLATES["custom"]
        mode = (auth_mode or tpl.get("auth_mode") or "api_key").strip()
        if mode not in {"api_key", "login"}:
            mode = "api_key"
        if mode == "login":
            resolved_env = (env_key or "").strip()
        else:
            resolved_env = (env_key or tpl.get("env_key") or "ANTHROPIC_API_KEY").strip()
        try:
            proxy_value = proxy_mod.normalise(proxy)
        except proxy_mod.ProxyError as error:
            raise ValueError(str(error)) from error
        profile = ClaudeProfile(
            id=profile_id,
            name=(name or profile_id).strip(),
            env_key=resolved_env,
            base_url=base_url.strip().rstrip("/"),
            model=model.strip(),
            template=template if template in TEMPLATES else "custom",
            auth_mode=mode,
            proxy=proxy_value,
            note=note.strip(),
        )
        if self.get(profile_id) is None:
            self.profiles.append(profile)
        else:
            self.profiles = [profile if p.id == profile_id else p for p in self.profiles]
        key = api_key.strip()
        if key:
            self.credentials.put(CRED_PREFIX + profile_id, key)
        self._refresh_secret_flags()
        if make_active or not self.active_id:
            self.active_id = profile_id
        self.save()
        return profile

    def delete(self, profile_id: str) -> bool:
        assert self.credentials is not None
        before = len(self.profiles)
        self.profiles = [p for p in self.profiles if p.id != profile_id]
        if len(self.profiles) == before:
            return False
        self.credentials.remove(CRED_PREFIX + profile_id)
        if self.active_id == profile_id:
            self.active_id = self.profiles[0].id if self.profiles else ""
        self.ensure_defaults()
        self.save()
        return True

    def set_active(self, profile_id: str) -> ClaudeProfile:
        profile = self.get(profile_id)
        if profile is None:
            raise ValueError(f"unknown profile '{profile_id}'")
        self.active_id = profile_id
        self.save()
        return profile

    def resolve_api_key(
        self,
        profile: ClaudeProfile,
        agent_accounts: list[ProviderAccount] | None = None,
    ) -> str:
        assert self.credentials is not None
        stored = self.credentials.get(CRED_PREFIX + profile.id)
        if stored:
            return stored
        if profile.env_key:
            from_env = os.environ.get(profile.env_key, "")
            if from_env:
                return from_env
        matched = match_agent_secret(
            base_url=profile.base_url,
            env_key=profile.env_key,
            accounts=agent_accounts,
            credentials=self.credentials,
        )
        if matched:
            return matched
        return os.environ.get("ANTHROPIC_API_KEY", "")

    def config_dir_for(self, profile: ClaudeProfile) -> Path:
        """Isolated Claude config dir so profiles do not share login state."""
        return Path.home() / ".claude-aih" / profile.id

    def launch_env(
        self,
        profile: ClaudeProfile,
        agent_accounts: list[ProviderAccount] | None = None,
        *,
        selected_model: str = "",
        selected_effort: str = "",
    ) -> dict[str, str]:
        env = {**os.environ}
        # Isolated Claude state per profile (login tokens, settings).
        cfg = self.config_dir_for(profile)
        cfg.mkdir(parents=True, exist_ok=True)
        env["CLAUDE_CONFIG_DIR"] = str(cfg)
        model = (selected_model or profile.model or "").strip()
        coding = is_kimi_coding(profile.base_url) or (
            "api.kimi.com" in (profile.base_url or "").lower()
            and "coding" in (profile.base_url or "").lower()
        )
        if profile.auth_mode != "login":
            key = self.resolve_api_key(profile, agent_accounts=agent_accounts)
            env_key = profile.env_key or "ANTHROPIC_API_KEY"
            if key:
                env[env_key] = key
                env["ANTHROPIC_API_KEY"] = key
                if coding:
                    # Kimi Coding docs use ANTHROPIC_API_KEY (x-api-key).
                    # AUTH_TOKEN (Bearer) is preferred by Claude Code when both
                    # exist and causes authentication_failed on this gateway.
                    env.pop("ANTHROPIC_AUTH_TOKEN", None)
                elif profile.base_url:
                    # DeepSeek / Moonshot Anthropic gateways expect AUTH_TOKEN.
                    env["ANTHROPIC_AUTH_TOKEN"] = key
            elif coding:
                env.pop("ANTHROPIC_AUTH_TOKEN", None)
        if profile.base_url:
            base = profile.base_url.strip()
            # Keep trailing slash on path-prefix Anthropic gateways.
            if coding:
                base = base.rstrip("/") + "/"
            elif "deepseek.com" in base.lower() and "anthropic" in base.lower():
                base = base.rstrip("/") + "/"
            env["ANTHROPIC_BASE_URL"] = base
        # Kimi Coding requires onboarding flags under CLAUDE_CONFIG_DIR.
        ensure_claude_third_party_onboarding(cfg)
        if model:
            env["ANTHROPIC_MODEL"] = model
            # Steer Claude Code defaults away from Anthropic opus on third-party.
            if profile.template in {"kimi", "kimi-platform", "deepseek", "glm", "custom"} or profile.base_url:
                env.setdefault("ANTHROPIC_DEFAULT_OPUS_MODEL", model)
                env.setdefault("ANTHROPIC_DEFAULT_SONNET_MODEL", model)
                env.setdefault("ANTHROPIC_DEFAULT_HAIKU_MODEL", model)
                env.setdefault("ANTHROPIC_DEFAULT_FABLE_MODEL", model)
                env.setdefault("CLAUDE_CODE_SUBAGENT_MODEL", model)
            if coding or profile.template == "kimi":
                # 1M context via env — do NOT put [1m] in the model id (API rejects it).
                if model in {"k3", "k3[1m]"} or "[1m]" in model:
                    env.setdefault("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "1048576")
                    env.setdefault("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "1048576")
                elif model == "k3-256k":
                    env.setdefault("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "262144")
                    env.setdefault("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "262144")
        effort = (selected_effort or "").strip()
        if effort:
            env["CLAUDE_CODE_EFFORT_LEVEL"] = effort
        return proxy_mod.apply_to_env(env, profile.proxy)

    def _unique_id(self, base: str) -> str:
        if not self.get(base):
            return base
        for index in range(2, 100):
            candidate = f"{base}-{index}"
            if not self.get(candidate):
                return candidate
        raise ValueError(f"could not allocate profile id from '{base}'")

    def import_from_account(
        self,
        account: ProviderAccount,
        *,
        make_active: bool = True,
    ) -> ClaudeProfile:
        """Create / refresh a Claude profile from an Agent ProviderAccount."""
        template = detect_claude_template(account)
        tpl = TEMPLATES.get(template) or TEMPLATES["custom"]
        preferred = profile_id_for_account("claude", account)
        profile_id = preferred if self.get(preferred) else self._unique_id(preferred)
        base_url = claude_base_url_for_account(account) or tpl.get("base_url", "")
        model = claude_model_for_account(account) or tpl.get("model", "") or ""
        secret = account_secret(account, self.credentials)
        return self.upsert(
            profile_id=profile_id,
            name=(account.note or account.id or profile_id).strip(),
            env_key=tpl.get("env_key", "ANTHROPIC_API_KEY"),
            base_url=base_url,
            model=model,
            template=template,
            auth_mode=tpl.get("auth_mode", "api_key"),
            api_key=secret,
            proxy=account.proxy or "",
            note=f"imported from Agent account {account.id}",
            make_active=make_active,
        )

    def is_logged_in(self, profile: ClaudeProfile) -> bool:
        """Best-effort check for Claude Code credentials under this profile config dir."""
        cfg = self.config_dir_for(profile)
        if profile.auth_mode != "login":
            return bool(self.resolve_api_key(profile))
        candidates = [
            cfg / ".credentials.json",
            cfg / "credentials.json",
            cfg / ".claude" / ".credentials.json",
        ]
        return any(path.is_file() for path in candidates)

    def launch_args(
        self,
        profile: ClaudeProfile,
        *,
        selected_model: str = "",
    ) -> list[str]:
        """Extra native CLI flags derived from the profile (never custom agent logic)."""
        args: list[str] = []
        model = (selected_model or profile.model or "").strip()
        if model:
            args.extend(["--model", model])
        return args


def _default_efforts_for(template: str, model_id: str) -> list[str]:
    for item in KNOWN_CLAUDE_MODELS.get(template, []):
        if item.get("id") == model_id:
            return list(item.get("efforts") or [])
    mid = (model_id or "").lower()
    if "kimi-k3" in mid or mid.startswith("k3"):
        return ["low", "medium", "high", "max"]
    if mid.startswith("deepseek") or mid.startswith("kimi") or mid.startswith("claude"):
        return ["low", "medium", "high"]
    return []


def _repair_one_profile(profile: ClaudeProfile) -> bool:
    """Mutate a single profile if its URL/model is a known bad import. Returns True if touched."""
    changed = False
    url = (profile.base_url or "").strip()
    lower = url.lower()
    host = ""
    path = ""
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        host = (parsed.hostname or "").lower()
        path = (parsed.path or "").lower()
    except Exception:  # noqa: BLE001
        pass

    # DeepSeek OpenAI ``/v1`` → Anthropic base.
    if "deepseek.com" in host and "/anthropic" not in lower:
        profile.base_url = "https://api.deepseek.com/anthropic"
        if profile.template not in TEMPLATES or profile.template in {"custom", "anthropic"}:
            profile.template = "deepseek"
        model = (profile.model or "").lower()
        if not model or model.startswith("claude") or "opus" in model or "sonnet" in model:
            profile.model = TEMPLATES["deepseek"]["model"]
        elif "flash" in model and "[1m]" not in profile.model and "v4" not in model:
            profile.model = "deepseek-v4-flash"
        elif model in {"deepseek-chat", "deepseek-reasoner"}:
            profile.model = "deepseek-v4-flash"
        changed = True

    # Template ``kimi`` means Coding API — never leave a Moonshot Platform URL.
    # (User profiles often imported as moonshot anthropic + coding key → auth fails.)
    if profile.template == "kimi" and (
        "moonshot." in lower
        or not url
        or ("api.kimi.com" not in host)
    ):
        desired = "https://api.kimi.com/coding/"
        if profile.base_url.rstrip("/") + "/" != desired:
            profile.base_url = desired
            changed = True
            url = desired
            lower = desired
            host = "api.kimi.com"
            path = "/coding/"

    # Kimi Coding: strip trailing /v1; never leave OpenAI-style coding/v1 for Claude.
    if is_kimi_coding(url) or ("api.kimi.com" in host and "coding" in path) or profile.template == "kimi":
        desired = "https://api.kimi.com/coding/"
        if profile.base_url.rstrip("/") + "/" != desired:
            profile.base_url = desired
            changed = True
        if profile.template not in {"kimi", "kimi-platform"}:
            profile.template = "kimi"
            changed = True
        model = (profile.model or "").strip()
        mapped = _map_kimi_claude_model(model)
        if mapped != model:
            profile.model = mapped
            changed = True

    # Old default ``kimi`` template pointed at Moonshot Anthropic. Keep
    # intentional ``kimi-platform`` profiles; only rewrite bare id ``kimi``
    # leftovers that still use the previous Moonshot default.
    if (
        profile.id == "kimi"
        and profile.template == "kimi"
        and "moonshot." in lower
        and "/anthropic" in lower
    ):
        profile.base_url = TEMPLATES["kimi"]["base_url"]
        profile.model = _map_kimi_claude_model(profile.model) or TEMPLATES["kimi"]["model"]
        changed = True

    return changed


def _map_kimi_claude_model(model: str) -> str:
    """Map Moonshot / Agent / docs ids onto live Kimi Coding API model ids.

    The Coding ``/v1/messages`` endpoint currently rejects literal ``k3[1m]``
    with authentication_error; use ``k3`` and rely on context-token env vars.
    """
    mid = (model or "").strip()
    lower = mid.lower()
    if not mid or lower in {"k3", "kimi-k3", "kimi-k3[1m]", "k3[1m]"}:
        return "k3"
    if lower in {"k3-256k", "kimi-k3-256k"}:
        return "k3-256k"
    if lower.startswith("claude"):
        return "k3"
    if lower.startswith("kimi-k3"):
        return "k3"
    return mid
