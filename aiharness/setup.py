"""Interactive configuration of accounts, models and roles.

The harness ships with **no models configured and no keys detected**. That is
deliberate. Guessing at a user's setup from environment variables means the
first request silently bills an account they did not mean to use, against a
model they did not choose. Better to start empty and make the user say what
they want, once.

Everything here exists so that saying it once is quick:

* add an account, and its credentials are checked against the live endpoint
  before anything is written;
* the model list is **fetched from that endpoint**, so the only models
  offerable are ones that account can actually serve;
* roles are assigned from the configured models, and an assignment that
  points at a model you have not configured is refused rather than saved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config.schema import (
    BUILTIN_ROLES,
    Config,
    EffortSpec,
    ModelDef,
    Pricing,
    ProviderAccount,
    RoleBinding,
)
from .constants import (
    HTTP_BAD_REQUEST,
    HTTP_GATEWAY_ERRORS,
    HTTP_NOT_FOUND,
    MODEL_FETCH_TIMEOUT,
    MODEL_LIST_LIMIT,
    SETUP_CONTEXT_CHOICES,
    SETUP_DEFAULT_CONTEXT,
    SETUP_DEFAULT_MAX_OUTPUT,
)
from .providers import proxy

#: Models an OpenAI-compatible endpoint lists that are not chat models.
NON_CHAT_HINTS = (
    "embedding", "whisper", "tts", "dall-e", "moderation", "rerank",
    "audio", "image", "search-", "similarity",
)
#: Model ids that hint at a reasoning model, so effort is offered by default.
REASONING_HINTS = ("reason", "-r1", "think", "o1", "o3", "o4", "gpt-5", "qwq")
#: Characters allowed in a local model alias.
ALIAS_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
VISION_MODES = frozenset({"auto", "on", "off"})


class SetupError(Exception):
    """Raised when a configuration step cannot be completed."""


@dataclass
class CatalogModel:
    """One entry from an account's ``/models`` response."""

    id: str
    #: ``True`` / ``False`` when the vendor reported it; ``None`` if unknown.
    supports_vision: bool | None = None


@dataclass
class AccountProbe:
    """What we learned by talking to an endpoint."""

    ok: bool
    detail: str
    models: list[str] = field(default_factory=list)
    #: Vision flags keyed by vendor model id (only known answers).
    vision: dict[str, bool] = field(default_factory=dict)
    #: Rich catalogue rows (same order as chat-filtered models when filled).
    catalogue: list[CatalogModel] = field(default_factory=list)

    @property
    def chat_models(self) -> list[str]:
        """Models that plausibly accept chat completions."""
        return [
            model
            for model in self.models
            if not any(hint in model.lower() for hint in NON_CHAT_HINTS)
        ]

    def catalogue_rows(self, limit: int = MODEL_LIST_LIMIT) -> list[dict[str, Any]]:
        """JSON-friendly catalogue for the GUI."""
        rows: list[dict[str, Any]] = []
        if self.catalogue:
            for entry in self.catalogue:
                if any(hint in entry.id.lower() for hint in NON_CHAT_HINTS):
                    continue
                rows.append({
                    "id": entry.id,
                    "supports_vision": entry.supports_vision,
                })
                if len(rows) >= limit:
                    break
            return rows
        for model_id in self.chat_models[:limit]:
            rows.append({
                "id": model_id,
                "supports_vision": self.vision.get(model_id),
            })
        return rows


def infer_vision_from_entry(entry: dict[str, Any]) -> bool | None:
    """Read vision capability from a ``/models`` row when the vendor exposes it."""
    for key in ("supports_image_in", "supports_vision", "supports_images"):
        if key in entry:
            return bool(entry.get(key))
    for key in ("modalities", "input_modalities", "supported_modalities"):
        mods = entry.get(key)
        if not isinstance(mods, list):
            continue
        lowered = {str(item).lower() for item in mods}
        if lowered & {"image", "vision", "image_url", "input_image"}:
            return True
        if lowered and lowered <= {"text", "txt"}:
            return False
    arch = entry.get("architecture")
    if isinstance(arch, dict):
        modality = str(arch.get("modality") or "").lower()
        if "image" in modality or "vision" in modality:
            return True
        inputs = arch.get("input_modalities") or arch.get("modality")
        if isinstance(inputs, list):
            lowered = {str(item).lower() for item in inputs}
            if lowered & {"image", "vision"}:
                return True
    return None


async def probe_and_list(account: ProviderAccount) -> AccountProbe:
    """Check credentials and fetch the model catalogue.

    Args:
      account: The account to test. Nothing is written anywhere.

    Returns:
      An :class:`AccountProbe`. ``ok`` is False when the endpoint rejected
      the credentials or could not be reached; the catalogue may still be
      empty on gateways that do not implement ``/models``.
    """
    url = account.base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {account.api_key}"} if account.api_key else {}
    headers.update(account.headers)

    try:
        async with httpx.AsyncClient(
            timeout=MODEL_FETCH_TIMEOUT, **proxy.client_kwargs(account)
        ) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as error:
        return AccountProbe(ok=False, detail=proxy.explain_failure(account, error))

    if response.status_code in (401, 403):
        return AccountProbe(ok=False, detail=f"credentials rejected ({response.status_code})")
    if response.status_code == HTTP_NOT_FOUND:
        # Common on gateways that only expose /chat/completions. The account
        # may still work; we just cannot enumerate its models.
        return AccountProbe(
            ok=True,
            detail="reachable, but it does not list models — add model ids by hand",
        )
    if response.status_code in HTTP_GATEWAY_ERRORS:
        # A proxy that is running but cannot reach the target answers for it,
        # so the failure arrives as a status code rather than a connection
        # error and mentions nothing about the tunnel it came through.
        return AccountProbe(
            ok=False,
            detail=proxy.explain_failure(
                account, RuntimeError(f"HTTP {response.status_code}（网关错误）")
            ),
        )
    if response.status_code >= HTTP_BAD_REQUEST:
        return AccountProbe(ok=False, detail=f"HTTP {response.status_code}")

    try:
        payload = response.json()
    except ValueError:
        return AccountProbe(ok=True, detail="reachable, but /models was not JSON")

    ids: list[str] = []
    vision: dict[str, bool] = {}
    catalogue_rows: list[CatalogModel] = []
    for entry in payload.get("data") or []:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        model_id = str(entry["id"])
        ids.append(model_id)
        flagged = infer_vision_from_entry(entry)
        if flagged is not None:
            vision[model_id] = flagged
        catalogue_rows.append(CatalogModel(id=model_id, supports_vision=flagged))
    ids = sorted(ids)
    return AccountProbe(
        ok=True,
        detail=f"ok, {len(ids)} model(s) listed",
        models=ids,
        vision=vision,
        catalogue=catalogue_rows,
    )


def suggest_alias(model_id: str, existing: set[str]) -> str:
    """Propose a short local alias for a vendor model id.

    Vendor ids are long and often namespaced (``deepseek-ai/DeepSeek-V3``);
    the alias is what the user types in ``/model``, so it should be short.
    """
    tail = model_id.rsplit("/", maxsplit=1)[-1].lower()
    tail = re.sub(r"[^a-zA-Z0-9.-]+", "-", tail).strip("-") or "model"
    if tail not in existing:
        return tail
    for suffix in range(2, 100):
        candidate = f"{tail}-{suffix}"
        if candidate not in existing:
            return candidate
    raise SetupError(f"cannot find a free alias for {model_id}")


def looks_like_reasoning_model(model_id: str) -> bool:
    """Whether to offer an effort parameter for this model by default."""
    lowered = model_id.lower()
    return any(hint in lowered for hint in REASONING_HINTS)


def build_account(
    account_id: str,
    base_url: str,
    api_key: str,
    *,
    api_key_env: str = "",
    headers: dict[str, str] | None = None,
) -> ProviderAccount:
    """Validate and construct an account.

    Raises:
      SetupError: If the id or URL is unusable.
    """
    account_id = account_id.strip()
    if not ALIAS_PATTERN.match(account_id):
        raise SetupError(
            f"'{account_id}' is not a usable account id — letters, digits, "
            f"dot, dash and underscore only"
        )
    base_url = base_url.strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise SetupError(f"'{base_url}' must start with http:// or https://")
    if not base_url.endswith("/v1") and "/v" not in base_url.rsplit("/", 1)[-1]:
        # Not fatal — some gateways differ — but it is the usual mistake.
        base_url = f"{base_url}/v1"
    return ProviderAccount(
        id=account_id,
        base_url=base_url,
        api_key=api_key.strip(),
        api_key_env=api_key_env.strip(),
        headers=headers or {},
    )


def build_model(
    alias: str,
    model_id: str,
    account_ids: list[str],
    *,
    context: int = SETUP_DEFAULT_CONTEXT,
    reasoning: bool | None = None,
    supports_vision: bool | None = None,
) -> ModelDef:
    """Construct a model definition with workable defaults.

    Pricing is left at zero on purpose: a guessed price produces a confidently
    wrong cost readout, which is worse than an obviously absent one.

    Args:
      supports_vision: Explicit probe result. ``None`` falls back to name
        heuristics (Kimi K3, GPT-4o, …). Never invents vision for DeepSeek V4.
    """
    alias = alias.strip()
    if not ALIAS_PATTERN.match(alias):
        raise SetupError(f"'{alias}' is not a usable alias")
    if not account_ids:
        raise SetupError(f"model '{alias}' needs at least one account")

    if reasoning is None:
        reasoning = looks_like_reasoning_model(model_id)
    effort = (
        EffortSpec(mode="reasoning_effort", levels={"low": "low", "medium": "medium", "high": "high"})
        if reasoning
        else EffortSpec(mode="none")
    )
    # Offer every plausible size, not just the default, so the user can pick
    # a smaller window to save money or a larger one if the model supports it.
    windows = sorted({*SETUP_CONTEXT_CHOICES, context})
    candidate = ModelDef(
        id=alias,
        model=model_id,
        accounts=list(account_ids),
        context_windows=windows,
        default_context=context,
        max_output_tokens=SETUP_DEFAULT_MAX_OUTPUT,
        effort=effort,
        default_effort="medium" if reasoning else "",
        pricing=Pricing(),
        vision_mode="auto",
    )
    from .session.attachments import infer_vision_capability

    if supports_vision is None:
        candidate.supports_vision = infer_vision_capability(candidate)
    else:
        candidate.supports_vision = bool(supports_vision)
    return candidate


def normalize_vision_mode(raw: str | None) -> str:
    mode = str(raw or "auto").strip().lower()
    if mode in ("force_on", "true", "1", "yes"):
        return "on"
    if mode in ("force_off", "false", "0", "no"):
        return "off"
    if mode in VISION_MODES:
        return mode
    return "auto"


def assign_role(config: Config, role: str, spec: str) -> RoleBinding:
    """Bind a role to a model, refusing anything not configured.

    Args:
      config: The configuration to modify.
      role: Role name, e.g. ``main`` or ``cheap``.
      spec: ``model`` or ``model@account``.
    """
    raw = (spec or "").strip()
    if not raw:
        raise SetupError("model spec is empty")
    account_id: str | None = None
    model_id = raw
    if "@" in raw:
        model_id, account_id = raw.split("@", 1)
        model_id, account_id = model_id.strip(), account_id.strip()
    model = config.model(model_id)
    if model is None:
        known = ", ".join(m.id for m in config.models) or "(none)"
        raise SetupError(
            f"no model '{model_id}' — not configured. Configured models: {known}"
        )
    if account_id:
        if config.account(account_id) is None:
            raise SetupError(f"unknown account '{account_id}'")
        if account_id not in model.accounts:
            raise SetupError(
                f"account '{account_id}' does not serve model '{model_id}'"
            )
    binding = RoleBinding(model=model.id, account=account_id or None)
    config.roles[role] = binding
    return binding


def unassigned_roles(config: Config) -> list[str]:
    """Builtin roles that still have no binding."""
    return [role for role in BUILTIN_ROLES if role not in config.roles]


def readiness(config: Config) -> tuple[bool, list[str]]:
    """Whether the config can start a normal chat, plus human-readable gaps."""
    problems: list[str] = []
    if not config.accounts:
        problems.append("no API accounts configured")
    if not config.models:
        problems.append("no models configured")
    if "main" not in config.roles:
        problems.append("default conversation model (main) is not assigned")
    return (not problems, problems)


def role_table(config: Config) -> list[tuple[str, str, bool]]:
    """Rows for the settings role list: (role, label, explicit)."""
    rows: list[tuple[str, str, bool]] = []
    default = config.roles.get("main")
    default_label = default.describe() if default else ""
    for role in BUILTIN_ROLES:
        binding = config.roles.get(role)
        if binding is not None:
            rows.append((role, binding.describe(), True))
        elif default is not None:
            rows.append((role, f"→ 默认对话模型 ({default_label})", False))
        else:
            rows.append((role, "(unassigned)", False))
    return rows


def catalogue(probe: AccountProbe, limit: int = MODEL_LIST_LIMIT) -> list[str]:
    """The selectable model list for an account, trimmed for display."""
    return probe.chat_models[:limit]


def describe_accounts(config: Config) -> list[dict[str, Any]]:
    """Account summaries for the setup screen."""
    return [
        {
            "id": account.id,
            "base_url": account.base_url,
            "key": account.masked_key(),
            "models": [m.id for m in config.models if account.id in m.accounts],
            "enabled": account.enabled,
        }
        for account in config.accounts
    ]
