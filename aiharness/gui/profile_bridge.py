"""Bridge Agent ProviderAccount credentials into Codex / Claude profiles."""

from __future__ import annotations

import re
from typing import Any, Iterable
from urllib.parse import urlparse

from ..config.schema import ProviderAccount
from ..credentials import CredentialStore

ID_SAFE = re.compile(r"[^a-zA-Z0-9_-]+")

_MOONSHOT_HOSTS = frozenset({"api.moonshot.cn", "api.moonshot.ai"})
_GLM_HOSTS = frozenset({"open.bigmodel.cn"})
_DEEPSEEK_HOSTS = frozenset({"api.deepseek.com"})


def _norm_url(url: str) -> str:
    return (url or "").strip().rstrip("/").lower()


def _host(url: str) -> str:
    raw = _norm_url(url)
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        return (urlparse(raw).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def _path(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        return (urlparse(raw).path or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def is_kimi_coding(url: str) -> bool:
    """Kimi Coding OpenAI/Anthropic endpoints (api.kimi.com/.../coding...)."""
    host = _host(url)
    path = _path(url)
    return "api.kimi.com" in host and "coding" in path


def vendor_family(url: str) -> str:
    """Coarse vendor bucket so OpenAI vs Anthropic path variants still match."""
    if is_kimi_coding(url):
        return "kimi-coding"
    host = _host(url)
    if host in _MOONSHOT_HOSTS or "moonshot" in host:
        return "moonshot"
    # Non-coding kimi hosts (legacy / platform aliases).
    if "kimi" in host:
        return "moonshot"
    if host in _DEEPSEEK_HOSTS or "deepseek" in host:
        return "deepseek"
    if host in _GLM_HOSTS or "bigmodel" in host or "zhipu" in host:
        return "glm"
    if "generativelanguage.googleapis" in host or "gemini" in host:
        return "gemini"
    if host in {"api.x.ai"} or "grok" in host:
        return "grok"
    if "anthropic" in host:
        return "anthropic"
    return ""


def account_secret(account: ProviderAccount, credentials: CredentialStore | None = None) -> str:
    """Live API key for an Agent account (pasted store or in-memory)."""
    if account.api_key:
        return account.api_key
    store = credentials or CredentialStore()
    return store.get(account.id) or ""


def match_agent_secret(
    *,
    base_url: str = "",
    env_key: str = "",
    accounts: Iterable[ProviderAccount] | None = None,
    credentials: CredentialStore | None = None,
) -> str:
    """Find an Agent account key that matches a panel profile endpoint."""
    if not accounts:
        return ""
    store = credentials or CredentialStore()
    want_url = _norm_url(base_url)
    want_env = (env_key or "").strip()
    want_family = vendor_family(base_url)

    # 1) Exact base URL
    for account in accounts:
        if not account.enabled:
            continue
        if want_url and _norm_url(account.base_url) == want_url:
            secret = account_secret(account, store)
            if secret:
                return secret

    # 2) Same vendor family (e.g. Moonshot /v1 ↔ /anthropic, Coding /v1 ↔ /coding)
    if want_family:
        for account in accounts:
            if not account.enabled:
                continue
            if vendor_family(account.base_url) == want_family:
                secret = account_secret(account, store)
                if secret:
                    return secret

    # 3) Matching env var name
    if want_env:
        for account in accounts:
            if not account.enabled:
                continue
            if account.api_key_env == want_env:
                secret = account_secret(account, store)
                if secret:
                    return secret
    return ""


def detect_codex_template(account: ProviderAccount) -> str:
    family = vendor_family(account.base_url)
    if family in {"moonshot", "kimi-coding"}:
        return "kimi"
    if family == "glm":
        return "glm"
    if family == "gemini":
        return "gemini"
    if family == "grok":
        return "grok"
    return "custom"


def detect_claude_template(account: ProviderAccount) -> str:
    family = vendor_family(account.base_url)
    if family == "kimi-coding":
        return "kimi"
    if family == "moonshot":
        return "kimi-platform"
    if family == "deepseek":
        return "deepseek"
    if family == "glm":
        return "glm"
    if family == "anthropic" or not _norm_url(account.base_url):
        return "anthropic"
    return "custom"


def claude_base_url_for_account(account: ProviderAccount) -> str:
    """Map an Agent (often OpenAI-style) URL to Claude Code's Anthropic base."""
    family = vendor_family(account.base_url)
    host = _host(account.base_url)
    if family == "kimi-coding":
        # Anthropic SDK appends /v1/messages — do not keep /coding/v1.
        return "https://api.kimi.com/coding/"
    if family == "moonshot":
        if host.endswith(".ai"):
            return "https://api.moonshot.ai/anthropic"
        return "https://api.moonshot.cn/anthropic"
    if family == "deepseek":
        return "https://api.deepseek.com/anthropic"
    if family == "glm":
        # Keep whatever the user already set if it looks Anthropic-shaped.
        url = _norm_url(account.base_url)
        if "anthropic" in url:
            return account.base_url.strip().rstrip("/")
        return ""
    return account.base_url.strip().rstrip("/")


def claude_model_for_account(account: ProviderAccount) -> str:
    """Pick a Claude Code model id for an Agent account."""
    family = vendor_family(account.base_url)
    # ProviderAccount has no model field; infer from note / id hints.
    hint = f"{account.note or ''} {account.id or ''}".lower()
    if family == "kimi-coding":
        if "256k" in hint:
            return "k3-256k"
        if "highspeed" in hint or "high-speed" in hint:
            return "kimi-for-coding-highspeed"
        if "kimi-for-coding" in hint or "for-coding" in hint:
            return "kimi-for-coding"
        # Live Coding API rejects literal k3[1m]; use k3 + context env vars.
        return "k3"
    if family == "moonshot":
        if "k2.7" in hint or "k2-7" in hint:
            return "kimi-k2.7"
        return "kimi-k3[1m]"
    if family == "deepseek":
        if "flash" in hint:
            return "deepseek-v4-flash"
        if "pro" in hint:
            return "deepseek-v4-pro[1m]"
        return "deepseek-v4-flash"
    return ""


def codex_model_for_account(account: ProviderAccount, template_default: str = "") -> str:
    """Pick a Codex model id for an Agent account."""
    family = vendor_family(account.base_url)
    hint = f"{account.note or ''} {account.id or ''}".lower()
    if family == "kimi-coding":
        if "highspeed" in hint or "high-speed" in hint:
            return "kimi-for-coding-highspeed"
        if "256k" in hint or "256-k" in hint:
            return "k3-256k"
        if "kimi-for-coding" in hint or "for-coding" in hint:
            return "kimi-for-coding"
        return "k3"
    if family == "moonshot":
        return template_default or "kimi-k3"
    return template_default or ""


def profile_id_for_account(prefix: str, account: ProviderAccount) -> str:
    raw = f"{prefix}-{account.id}".strip("-")
    cleaned = ID_SAFE.sub("-", raw).strip("-")
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"a-{cleaned}" if cleaned else f"{prefix}-import"
    return cleaned[:48]


def account_public(account: ProviderAccount) -> dict[str, Any]:
    return {
        "id": account.id,
        "base_url": account.base_url,
        "env": account.api_key_env,
        "proxy": account.proxy or "",
        "note": account.note or "",
        "has_key": bool(account.api_key or CredentialStore().get(account.id)),
        "codex_template": detect_codex_template(account),
        "claude_template": detect_claude_template(account),
    }
