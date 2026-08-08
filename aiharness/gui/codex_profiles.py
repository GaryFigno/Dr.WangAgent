"""Durable Codex provider profiles for the GUI Codex panel.

Profiles live in a small JSON file under the user config dir. Pasted API keys
go into :class:`~aiharness.credentials.CredentialStore` under ``codex:<id>``
so nothing secret is written into Codex ``config.toml`` templates.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir

from ..config.schema import ProviderAccount
from ..credentials import CredentialStore
from ..providers import proxy as proxy_mod
from .profile_bridge import (
    account_secret,
    codex_model_for_account,
    detect_codex_template,
    match_agent_secret,
    profile_id_for_account,
)

PROFILES_FILE = "codex_profiles.json"
CRED_PREFIX = "codex:"
ID_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,47}$")

# Codex CLI (2026+) rejects wire_api="chat"; use responses.
DEFAULT_WIRE_API = "responses"

KIMI_LEGACY_TEMPLATE = f"""\
# AIHarness Codex panel — legacy Kimi home
model = "kimi-k3"
model_provider = "kimi"

[model_providers.kimi]
name = "Kimi"
base_url = "https://api.moonshot.cn/v1"
env_key = "KIMI_API_KEY"
wire_api = "{DEFAULT_WIRE_API}"
"""

#: Fallback catalog when the provider ``/models`` endpoint is unreachable.
#: Codex ``model/list`` only knows OpenAI models, so custom providers need this.
KNOWN_PROVIDER_MODELS: dict[str, list[dict[str, Any]]] = {
    "kimi": [
        {
            "id": "k3",
            "label": "k3 (Kimi Coding)",
            "efforts": ["low", "high", "max"],
            "default_effort": "max",
        },
        {
            "id": "k3-256k",
            "label": "k3-256k (Kimi Coding)",
            "efforts": ["low", "high", "max"],
            "default_effort": "max",
        },
        {
            "id": "kimi-for-coding",
            "label": "kimi-for-coding",
            "efforts": ["low", "high", "max"],
            "default_effort": "high",
        },
        {
            "id": "kimi-for-coding-highspeed",
            "label": "kimi-for-coding-highspeed",
            "efforts": ["low", "high", "max"],
            "default_effort": "medium",
        },
        {
            "id": "kimi-k3",
            "label": "kimi-k3",
            "efforts": ["low", "high", "max"],
            "default_effort": "max",
        },
        {
            "id": "kimi-k2.7-code",
            "label": "kimi-k2.7-code",
            "efforts": ["low", "medium", "high"],
            "default_effort": "high",
        },
        {
            "id": "kimi-k2.7-code-highspeed",
            "label": "kimi-k2.7-code-highspeed",
            "efforts": ["low", "medium", "high"],
            "default_effort": "medium",
        },
        {
            "id": "kimi-k2.6",
            "label": "kimi-k2.6",
            "efforts": ["low", "medium", "high"],
            "default_effort": "high",
        },
        {
            "id": "kimi-k2.5",
            "label": "kimi-k2.5",
            "efforts": ["low", "medium", "high"],
            "default_effort": "high",
        },
    ],
}


def ensure_kimi_home_compat(path: Path | None = None) -> Path:
    """Create legacy ``~/.codex-kimi`` for older tests / manual use."""
    home = path or (Path.home() / ".codex-kimi")
    home.mkdir(parents=True, exist_ok=True)
    config = home / "config.toml"
    if not config.exists():
        config.write_text(KIMI_LEGACY_TEMPLATE, encoding="utf-8")
    return home

#: Built-in OpenAI-compatible endpoint templates.
TEMPLATES: dict[str, dict[str, str]] = {
    "kimi": {
        "name": "Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "kimi-k3",
        "env_key": "KIMI_API_KEY",
        "wire_api": DEFAULT_WIRE_API,
        "provider_id": "kimi",
    },
    "glm": {
        "name": "GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4.5",
        "env_key": "GLM_API_KEY",
        "wire_api": DEFAULT_WIRE_API,
        "provider_id": "glm",
    },
    "gemini": {
        "name": "Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "model": "gemini-2.5-pro",
        "env_key": "GEMINI_API_KEY",
        "wire_api": DEFAULT_WIRE_API,
        "provider_id": "gemini",
    },
    "grok": {
        "name": "Grok",
        "base_url": "https://api.x.ai/v1",
        "model": "grok-3",
        "env_key": "XAI_API_KEY",
        "wire_api": DEFAULT_WIRE_API,
        "provider_id": "grok",
    },
    "custom": {
        "name": "Custom",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4.1",
        "env_key": "OPENAI_API_KEY",
        "wire_api": DEFAULT_WIRE_API,
        "provider_id": "custom",
    },
}


@dataclass
class CodexProfile:
    id: str
    name: str
    base_url: str
    model: str
    env_key: str = ""
    wire_api: str = DEFAULT_WIRE_API
    provider_id: str = "custom"
    template: str = "custom"
    #: "" = inherit system · "direct" = no proxy · else proxy URL (e.g. Clash).
    proxy: str = ""
    note: str = ""
    #: True when a pasted secret exists in the credential store (never the key itself).
    has_secret: bool = False

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data["proxy_label"] = proxy_mod.describe_setting(self.proxy)
        return data


@dataclass
class CodexProfileStore:
    """Load / save Codex profiles and materialize a CODEX_HOME for one of them."""

    path: Path | None = None
    credentials: CredentialStore | None = None
    active_id: str = ""
    profiles: list[CodexProfile] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.path is None:
            override = os.environ.get("AIH_CODEX_PROFILES_FILE")
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
        profiles: list[CodexProfile] = []
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                try:
                    proxy_value = proxy_mod.normalise(str(item.get("proxy") or ""))
                except proxy_mod.ProxyError:
                    proxy_value = ""
                profiles.append(
                    CodexProfile(
                        id=str(item["id"]),
                        name=str(item.get("name") or item["id"]),
                        base_url=str(item.get("base_url") or ""),
                        model=str(item.get("model") or ""),
                        env_key=str(item.get("env_key") or ""),
                        wire_api=_normalize_wire_api(str(item.get("wire_api") or DEFAULT_WIRE_API)),
                        provider_id=str(item.get("provider_id") or "custom"),
                        template=str(item.get("template") or "custom"),
                        proxy=proxy_value,
                        note=str(item.get("note") or ""),
                    )
                )
        self.profiles = profiles
        self.active_id = str(payload.get("active_id") or "")
        self.ensure_defaults()
        self._refresh_secret_flags()

    def save(self) -> Path:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "active_id": self.active_id,
            "profiles": [
                {
                    "id": p.id,
                    "name": p.name,
                    "base_url": p.base_url,
                    "model": p.model,
                    "env_key": p.env_key,
                    "wire_api": p.wire_api,
                    "provider_id": p.provider_id,
                    "template": p.template,
                    "proxy": p.proxy,
                    "note": p.note,
                }
                for p in self.profiles
            ],
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.path

    def ensure_defaults(self) -> None:
        """Seed one profile per major template when the store is empty."""
        if self.profiles:
            if not self.active_id or self.get(self.active_id) is None:
                self.active_id = self.profiles[0].id
            return
        for key in ("kimi", "glm", "gemini", "grok"):
            tpl = TEMPLATES[key]
            self.profiles.append(
                CodexProfile(
                    id=key,
                    name=tpl["name"],
                    base_url=tpl["base_url"],
                    model=tpl["model"],
                    env_key=tpl["env_key"],
                    wire_api=tpl["wire_api"],
                    provider_id=tpl["provider_id"],
                    template=key,
                )
            )
        self.active_id = "kimi"
        self.save()

    def _refresh_secret_flags(self) -> None:
        assert self.credentials is not None
        for profile in self.profiles:
            profile.has_secret = bool(self.credentials.get(CRED_PREFIX + profile.id))

    def get(self, profile_id: str) -> CodexProfile | None:
        return next((p for p in self.profiles if p.id == profile_id), None)

    def list_public(self) -> list[dict[str, Any]]:
        self._refresh_secret_flags()
        return [p.public() for p in self.profiles]

    def templates_public(self) -> list[dict[str, str]]:
        return [{"id": key, **value} for key, value in TEMPLATES.items()]

    def upsert(
        self,
        *,
        profile_id: str,
        name: str,
        base_url: str,
        model: str,
        env_key: str = "",
        wire_api: str = DEFAULT_WIRE_API,
        provider_id: str = "",
        template: str = "custom",
        api_key: str = "",
        proxy: str = "",
        note: str = "",
        make_active: bool = False,
    ) -> CodexProfile:
        assert self.credentials is not None
        profile_id = profile_id.strip()
        if not ID_RE.match(profile_id):
            raise ValueError("profile id must be letters/digits/_/- and start with a letter")
        if not base_url.strip() or not model.strip():
            raise ValueError("base_url and model are required")
        tpl = TEMPLATES.get(template) or TEMPLATES["custom"]
        provider = (provider_id or tpl["provider_id"] or profile_id).strip()
        # Provider ids reserved by Codex cannot be overridden.
        if provider in {"openai", "ollama", "lmstudio"}:
            provider = f"p_{provider}"
        try:
            proxy_value = proxy_mod.normalise(proxy)
        except proxy_mod.ProxyError as error:
            raise ValueError(str(error)) from error
        existing = self.get(profile_id)
        profile = CodexProfile(
            id=profile_id,
            name=(name or profile_id).strip(),
            base_url=base_url.strip().rstrip("/"),
            model=model.strip(),
            env_key=(env_key or tpl["env_key"]).strip(),
            wire_api=_normalize_wire_api(wire_api or tpl.get("wire_api") or DEFAULT_WIRE_API),
            provider_id=provider,
            template=template if template in TEMPLATES else "custom",
            proxy=proxy_value,
            note=note.strip(),
        )
        if existing is None:
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

    def set_active(self, profile_id: str) -> CodexProfile:
        profile = self.get(profile_id)
        if profile is None:
            raise ValueError(f"unknown profile '{profile_id}'")
        self.active_id = profile_id
        self.save()
        return profile

    def resolve_api_key(
        self,
        profile: CodexProfile,
        agent_accounts: list[ProviderAccount] | None = None,
    ) -> str:
        """Return the live key from credential store, env, or matching Agent account."""
        assert self.credentials is not None
        stored = self.credentials.get(CRED_PREFIX + profile.id)
        if stored:
            return stored
        if profile.env_key:
            from_env = os.environ.get(profile.env_key, "")
            if from_env:
                return from_env
        return match_agent_secret(
            base_url=profile.base_url,
            env_key=profile.env_key,
            accounts=agent_accounts,
            credentials=self.credentials,
        )

    def home_for(self, profile_id: str) -> Path:
        return Path.home() / ".codex-aih" / profile_id

    def materialize(
        self,
        profile: CodexProfile,
        *,
        base_url_override: str | None = None,
    ) -> Path:
        """Write a CODEX_HOME config.toml for this profile and return the home path.

        ``base_url_override`` is used when a local Responses↔Chat bridge is
        active; the profile's own ``base_url`` stays as the real upstream for UI.
        """
        home = self.home_for(profile.id)
        home.mkdir(parents=True, exist_ok=True)
        provider = profile.provider_id or "custom"
        env_key = profile.env_key or f"AIH_CODEX_{profile.id.upper().replace('-', '_')}_KEY"
        base_url = (base_url_override or profile.base_url).strip().rstrip("/")
        config = f"""\
# Generated by AIHarness Codex panel — profile {profile.id}
# Do not put secrets in this file. Keys come from env / credential store.

model = {json.dumps(profile.model)}
model_provider = {json.dumps(provider)}

[model_providers.{provider}]
name = {json.dumps(profile.name)}
base_url = {json.dumps(base_url)}
env_key = {json.dumps(env_key)}
wire_api = {json.dumps(_normalize_wire_api(profile.wire_api))}
"""
        (home / "config.toml").write_text(config, encoding="utf-8")
        return home

    def repair_homes(self) -> int:
        """Rewrite outdated wire_api=chat in generated CODEX_HOME configs.

        Does **not** rewrite Kimi Coding URLs to Moonshot — those keys are not
        interchangeable.
        """
        root = Path.home() / ".codex-aih"
        if not root.is_dir():
            return 0
        fixed = 0
        for config in root.glob("*/config.toml"):
            try:
                text = config.read_text(encoding="utf-8")
            except OSError:
                continue
            if 'wire_api = "chat"' not in text and "wire_api = 'chat'" not in text:
                continue
            text = text.replace('wire_api = "chat"', f'wire_api = "{DEFAULT_WIRE_API}"')
            text = text.replace("wire_api = 'chat'", f"wire_api = '{DEFAULT_WIRE_API}'")
            try:
                config.write_text(text, encoding="utf-8")
                fixed += 1
            except OSError:
                continue
        # Also refresh in-memory profile wire_api.
        changed = False
        for profile in self.profiles:
            if profile.wire_api == "chat":
                profile.wire_api = DEFAULT_WIRE_API
                changed = True
        if changed:
            self.save()
        return fixed

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
    ) -> CodexProfile:
        """Create / refresh a Codex profile from an Agent ProviderAccount."""
        template = detect_codex_template(account)
        tpl = TEMPLATES.get(template) or TEMPLATES["custom"]
        preferred = profile_id_for_account("codex", account)
        profile_id = preferred if self.get(preferred) else self._unique_id(preferred)
        # Keep Kimi Coding ``…/coding/v1`` as-is; the Responses bridge handles it.
        base_url = (account.base_url or tpl.get("base_url", "")).strip().rstrip("/")
        model = codex_model_for_account(account, tpl.get("model", "") or "gpt-4.1") or (
            tpl.get("model", "") or "gpt-4.1"
        )
        secret = account_secret(account, self.credentials)
        return self.upsert(
            profile_id=profile_id,
            name=(account.note or account.id or profile_id).strip(),
            base_url=base_url or tpl["base_url"],
            model=model,
            env_key=account.api_key_env or tpl.get("env_key", ""),
            wire_api=tpl.get("wire_api", DEFAULT_WIRE_API),
            provider_id=tpl.get("provider_id", ""),
            template=template,
            api_key=secret,
            proxy=account.proxy or "",
            note=f"imported from Agent account {account.id}",
            make_active=make_active,
        )

    def launch_env(
        self,
        profile: CodexProfile,
        agent_accounts: list[ProviderAccount] | None = None,
        *,
        base_url_override: str | None = None,
    ) -> dict[str, str]:
        """Environment for the Codex child process using this profile."""
        home = self.materialize(profile, base_url_override=base_url_override)
        env = {**os.environ, "CODEX_HOME": str(home)}
        env_key = profile.env_key or f"AIH_CODEX_{profile.id.upper().replace('-', '_')}_KEY"
        key = self.resolve_api_key(profile, agent_accounts=agent_accounts)
        if key:
            env[env_key] = key
        # Convenience aliases for Kimi / Coding.
        if (
            profile.template == "kimi"
            or "moonshot" in profile.base_url
            or "api.kimi.com" in profile.base_url
        ):
            if key:
                env.setdefault("KIMI_API_KEY", key)
                env.setdefault("MOONSHOT_API_KEY", key)
        return proxy_mod.apply_to_env(env, profile.proxy)


def _normalize_wire_api(value: str) -> str:
    text = (value or "").strip().lower()
    if text in {"", "chat"}:
        return DEFAULT_WIRE_API
    return text
