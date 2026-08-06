"""Model/account selection, failover and cost accounting.

This is where "one model, several API accounts" is resolved.  A
:class:`Selection` names a model, optionally pins an account, and carries
the context-window and effort choices.  The router turns that into a live
provider, and on a retryable failure moves to the next account that can
serve the same model.
"""

from __future__ import annotations

import asyncio
import itertools
import random
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config.schema import Config, ModelDef, ProviderAccount, RoleBinding
from ..constants import (
    AUTH_FAILURE_COOLDOWN,
    HTTP_BAD_REQUEST,
    HTTP_MAX_CONNECTIONS,
    HTTP_MAX_KEEPALIVE,
    MAX_ATTEMPTS_PER_ACCOUNT,
    RATE_LIMIT_COOLDOWN,
    RETRY_BACKOFF_BASE,
    RETRY_BACKOFF_CEILING,
    ROUTE_ERROR_HISTORY,
    RPM_WINDOW_SECONDS,
    TOKENS_PER_MILLION,
)
from . import proxy, quirks
from .base import (
    AuthError,
    CompletionRequest,
    Provider,
    ProviderError,
    RateLimitError,
    StreamDone,
    StreamEvent,
    Usage,
)
from .openai_compat import OpenAICompatProvider


def _retry_backoff(attempt: int) -> float:
    """Exponential backoff with jitter so thundering herds do not sync up."""
    base = min(RETRY_BACKOFF_BASE * 2**attempt, RETRY_BACKOFF_CEILING)
    return base * (0.5 + random.random())


def _drop_rejected_parameter(model: ModelDef, req: CompletionRequest, error: ProviderError) -> str | None:
    """Strip the parameter an endpoint refused, so the retry can differ.

    Args:
      model: Marked so the rest of the session stops sending the parameter.
      req: Mutated in place; the retry reuses this object.
      error: The provider's refusal.

    Returns:
      The parameter dropped, or None when the error was not attributable to
      one — in which case the caller must let the error through rather than
      retry an identical request.
    """
    if error.status != HTTP_BAD_REQUEST:
        return None
    parameter = quirks.detect_rejected_parameter(str(error))
    if parameter is None or not quirks.disable_parameter(model, parameter):
        return None
    if parameter in ("temperature", "top_p"):
        req.temperature = None
    return parameter


def _persist_quirk(cfg: Config, model: ModelDef) -> bool:
    """Write the disabled support flag so the next launch does not rediscover it."""
    try:
        from ..config.loader import save_config

        live = cfg.model(model.id)
        if live is not None and live is not model:
            live.supports_temperature = model.supports_temperature
        save_config(cfg)
        return True
    except Exception:
        return False


class NoRouteError(ProviderError):
    pass


@dataclass
class Selection:
    """A fully-resolved "what do I call, and how" decision."""

    model_id: str
    account_id: str | None = None  # None = let the router pick
    context: int | None = None
    effort: str | None = None
    temperature: float | None = None

    def label(self) -> str:
        s = self.model_id
        if self.account_id:
            s += f"@{self.account_id}"
        if self.effort:
            s += f" ({self.effort})"
        return s

    @classmethod
    def parse(cls, spec: str, cfg: Config) -> Selection:
        """Parse ``model``, ``model@account``, or ``role:name``."""
        spec = spec.strip()
        if spec.startswith("role:"):
            binding = cfg.role(spec[5:])
            if not binding:
                raise NoRouteError(f"unknown role '{spec[5:]}'")
            return cls.from_binding(binding)
        account_id = None
        model_id = spec
        if "@" in spec:
            model_id, account_id = spec.split("@", 1)
        if not cfg.model(model_id):
            raise NoRouteError(f"unknown model '{model_id}'")
        if account_id and not cfg.account(account_id):
            raise NoRouteError(f"unknown account '{account_id}'")
        if account_id and account_id not in (cfg.model(model_id).accounts):
            raise NoRouteError(f"account '{account_id}' does not serve model '{model_id}'")
        return cls(model_id=model_id, account_id=account_id)

    @classmethod
    def from_binding(cls, binding: RoleBinding) -> Selection:
        return cls(
            model_id=binding.model,
            account_id=binding.account,
            context=binding.context,
            effort=binding.effort,
            temperature=binding.temperature,
        )

    @classmethod
    def for_session(cls, cfg: Config, session: object | None) -> Selection:
        """Conversation model: session picker first, else config default (``main``).

        ``roles.main`` is only the default for new chats. An open session's
        ``meta.model`` / ``meta.account`` (written by the dialog picker) win.
        """
        meta = getattr(session, "meta", None)
        model = getattr(meta, "model", "") or ""
        account = getattr(meta, "account", "") or ""
        if model:
            spec = f"{model}@{account}" if account else model
            try:
                return cls.parse(spec, cfg)
            except NoRouteError:
                pass
        binding = cfg.role("main")
        return cls.from_binding(binding) if binding else cls(model_id="")


@dataclass
class AccountState:
    """Local health bookkeeping; nothing is read back from the vendor."""

    in_flight: int = 0
    total_calls: int = 0
    failures: int = 0
    cooldown_until: float = 0.0
    recent_calls: list[float] = field(default_factory=list)

    def available(self, now: float, rpm_limit: int | None) -> bool:
        """Whether this account may be used right now."""
        if now < self.cooldown_until:
            return False
        if rpm_limit:
            window = [t for t in self.recent_calls if now - t < RPM_WINDOW_SECONDS]
            self.recent_calls = window
            if len(window) >= rpm_limit:
                return False
        return True


@dataclass
class CallRecord:
    model_id: str
    account_id: str
    usage: Usage
    cost: float
    duration: float
    role: str = ""


class UsageLedger:
    """Running token/cost totals for the session."""

    def __init__(self) -> None:
        self.records: list[CallRecord] = []

    def add(self, record: CallRecord) -> None:
        self.records.append(record)

    @property
    def total_cost(self) -> float:
        return sum(r.cost for r in self.records)

    @property
    def total_usage(self) -> Usage:
        total = Usage()
        for r in self.records:
            total = total + r.usage
        return total

    def by_model(self) -> dict[str, tuple[Usage, float, int]]:
        out: dict[str, tuple[Usage, float, int]] = {}
        for r in self.records:
            key = f"{r.model_id}@{r.account_id}"
            usage, cost, count = out.get(key, (Usage(), 0.0, 0))
            out[key] = (usage + r.usage, cost + r.cost, count + 1)
        return out

    def by_role(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for r in self.records:
            out[r.role or "-"] = out.get(r.role or "-", 0.0) + r.cost
        return out


def compute_cost(model: ModelDef, usage: Usage) -> float:
    """Price one call in USD.

    Cached input is billed at ``pricing.cached_input`` when the model declares
    one, since every backend that caches discounts it heavily.

    Args:
      model: The model definition carrying the price table.
      usage: Token counts reported by the backend.

    Returns:
      The cost in USD.
    """
    pricing = model.pricing
    cached = usage.cached_tokens
    fresh_input = max(usage.input_tokens - cached, 0)
    cached_rate = pricing.cached_input if pricing.cached_input is not None else pricing.input

    cost = fresh_input / TOKENS_PER_MILLION * pricing.input
    cost += usage.output_tokens / TOKENS_PER_MILLION * pricing.output
    cost += cached / TOKENS_PER_MILLION * cached_rate
    return cost


class Router:
    """Owns the HTTP client pool and picks accounts for models."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.ledger = UsageLedger()
        self._state: dict[str, AccountState] = {}
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._rr: dict[str, itertools.count] = {}
        #: Recoveries worth telling the user about, drained by the UI. A
        #: silent workaround is indistinguishable from a bug on the next run.
        self.notices: list[str] = []
        self._lock = asyncio.Lock()

    # -- plumbing ---------------------------------------------------------

    def _client_for(self, account: ProviderAccount) -> httpx.AsyncClient:
        client = self._clients.get(account.id)
        if client is None or client.is_closed:
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(account.timeout),
                limits=httpx.Limits(
                    max_connections=HTTP_MAX_CONNECTIONS,
                    max_keepalive_connections=HTTP_MAX_KEEPALIVE,
                ),
                # Pooled per account, so each account keeps its own route.
                **proxy.client_kwargs(account),
            )
            self._clients[account.id] = client
        return client

    async def reset_client(self, account_id: str) -> None:
        """Drop an account's pooled client so the next call rebuilds it.

        Connection settings — the proxy in particular — are fixed when the
        client is constructed, so changing one has no effect until the old
        client is gone.
        """
        client = self._clients.pop(account_id, None)
        if client is not None and not client.is_closed:
            await client.aclose()

    def state(self, account_id: str) -> AccountState:
        return self._state.setdefault(account_id, AccountState())

    # -- candidate ordering -----------------------------------------------

    def candidates(self, sel: Selection) -> list[ProviderAccount]:
        model = self.cfg.model(sel.model_id)
        if not model:
            raise NoRouteError(f"unknown model '{sel.model_id}'")

        accounts = self.cfg.accounts_for(sel.model_id)
        if sel.account_id:
            pinned = [a for a in accounts if a.id == sel.account_id]
            if not pinned:
                raise NoRouteError(
                    f"account '{sel.account_id}' is not enabled for model '{sel.model_id}'"
                )
            # A pin still allows the others as failover, but only after the pin.
            rest = [a for a in accounts if a.id != sel.account_id]
            return pinned + self._order(rest, sel.model_id)
        if not accounts:
            raise NoRouteError(f"model '{sel.model_id}' has no enabled accounts")
        return self._order(accounts, sel.model_id)

    def _order(self, accounts: list[ProviderAccount], model_id: str) -> list[ProviderAccount]:
        if not accounts:
            return []
        strategy = self.cfg.route_strategy
        now = time.time()
        healthy = [a for a in accounts if self.state(a.id).available(now, a.rpm_limit)]
        pool = healthy or accounts  # if everything is cooling down, try anyway

        if strategy == "round_robin":
            counter = self._rr.setdefault(model_id, itertools.count())
            offset = next(counter) % len(pool)
            return pool[offset:] + pool[:offset]
        if strategy == "least_used":
            return sorted(pool, key=lambda a: (self.state(a.id).in_flight, self.state(a.id).total_calls))
        if strategy == "random":
            shuffled = list(pool)
            random.shuffle(shuffled)
            return shuffled
        return sorted(pool, key=lambda a: a.priority)

    # -- request building -------------------------------------------------

    def build_request(
        self,
        sel: Selection,
        messages: list[Any],
        tools: list[dict[str, Any]] | None = None,
        *,
        stream: bool = True,
        max_tokens: int | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> CompletionRequest:
        model = self.cfg.model(sel.model_id)
        if not model:
            raise NoRouteError(f"unknown model '{sel.model_id}'")

        effort = sel.effort or model.default_effort
        body_extras: dict[str, Any] = {}
        if effort and model.effort.mode != "none":
            body_extras.update(model.effort.build(effort))
        if extra_body:
            body_extras.update(extra_body)

        temperature = sel.temperature
        if temperature is None and model.supports_temperature:
            temperature = 0.2 if tools else 0.7

        return CompletionRequest(
            messages=messages,
            tools=tools or [],
            max_tokens=max_tokens,
            temperature=temperature,
            stream=stream,
            extra_body=body_extras,
            tool_choice=tool_choice,
        )

    def provider_for(self, sel: Selection, account: ProviderAccount) -> Provider:
        model = self.cfg.model(sel.model_id)
        assert model is not None
        return OpenAICompatProvider(
            account,
            model,
            client=self._client_for(account),
            context_window=sel.context,
        )

    # -- execution --------------------------------------------------------

    async def stream(
        self,
        sel: Selection,
        req: CompletionRequest,
        *,
        role: str = "",
        max_attempts_per_account: int = MAX_ATTEMPTS_PER_ACCOUNT,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a completion, failing over across accounts.

        Failover only happens before any content has been emitted; once the
        caller has seen deltas we cannot silently restart on another account
        without duplicating output, so a mid-stream failure propagates.
        """
        model = self.cfg.model(sel.model_id)
        if not model:
            raise NoRouteError(f"unknown model '{sel.model_id}'")

        accounts = self.candidates(sel)
        errors: list[str] = []

        for account in accounts:
            for attempt in range(max_attempts_per_account):
                provider = self.provider_for(sel, account)
                state = self.state(account.id)
                started = time.time()
                emitted = False
                state.in_flight += 1
                state.total_calls += 1
                state.recent_calls.append(started)
                got_done = False
                try:
                    async for event in provider.stream(req):
                        emitted = True
                        if isinstance(event, StreamDone):
                            got_done = True
                            cost = compute_cost(model, event.usage)
                            self.ledger.add(
                                CallRecord(
                                    model_id=model.id,
                                    account_id=account.id,
                                    usage=event.usage,
                                    cost=cost,
                                    duration=time.time() - started,
                                    role=role,
                                )
                            )
                            state.failures = 0
                        yield event
                    if got_done:
                        return
                    # Truncated / empty stream with no completion frame.
                    state.failures += 1
                    errors.append(f"{account.id}: stream ended without completion")
                    if emitted:
                        raise ProviderError(
                            f"[{account.id}] stream ended without a completion",
                            retryable=False,
                        )
                    if attempt + 1 < max_attempts_per_account:
                        await asyncio.sleep(_retry_backoff(attempt))
                        continue
                    break
                except RateLimitError as error:
                    state.failures += 1
                    backoff = error.retry_after or min(
                        RETRY_BACKOFF_BASE * 2**attempt, RETRY_BACKOFF_CEILING
                    )
                    errors.append(f"{account.id}: rate limited")
                    state.cooldown_until = time.time() + max(backoff, RATE_LIMIT_COOLDOWN)
                    if emitted:
                        raise
                    # Prefer another account; if none remain, one same-account wait.
                    if attempt + 1 < max_attempts_per_account and len(accounts) == 1:
                        await asyncio.sleep(min(backoff, RETRY_BACKOFF_CEILING))
                        continue
                    break
                except AuthError as error:
                    state.failures += 1
                    state.cooldown_until = time.time() + AUTH_FAILURE_COOLDOWN
                    errors.append(str(error))
                    if emitted:
                        raise
                    break  # a bad key will not fix itself on retry
                except ProviderError as error:
                    state.failures += 1
                    # A 400 naming a parameter is not a transport failure: the
                    # request itself is unacceptable to this model. Retrying
                    # it unchanged would fail identically, so drop the knob
                    # and try once more before giving up on the turn.
                    dropped = None if emitted else _drop_rejected_parameter(model, req, error)
                    if dropped:
                        persisted = _persist_quirk(self.cfg, model)
                        self.notices.append(
                            quirks.explain(model, dropped, persisted=persisted)
                        )
                        continue
                    errors.append(str(error))
                    if emitted or not error.retryable:
                        raise
                    if attempt + 1 < max_attempts_per_account:
                        await asyncio.sleep(_retry_backoff(attempt))
                        continue
                    break
                finally:
                    state.in_flight -= 1

        recent = "\n  ".join(errors[-ROUTE_ERROR_HISTORY:])
        raise NoRouteError(f"all accounts failed for model '{sel.model_id}':\n  {recent}")

    async def complete(
        self, sel: Selection, req: CompletionRequest, *, role: str = ""
    ) -> StreamDone:
        last: StreamDone | None = None
        async for event in self.stream(sel, req, role=role):
            if isinstance(event, StreamDone):
                last = event
        if last is None:
            raise ProviderError("stream ended without a completion")
        return last

    async def ask(
        self,
        sel: Selection,
        messages: list[Any],
        *,
        role: str = "",
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> StreamDone:
        """One-shot helper used by workflows and internal utility calls."""
        req = self.build_request(sel, messages, tools=tools, stream=True, max_tokens=max_tokens)
        return await self.complete(sel, req, role=role)

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()
