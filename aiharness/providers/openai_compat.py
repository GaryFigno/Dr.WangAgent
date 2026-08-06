"""OpenAI-compatible chat-completions adapter.

Deliberately hand-rolled on httpx rather than the vendor SDK: gateways
disagree about optional fields, and we need to tolerate that rather than
fail validation.  Handles DeepSeek's ``reasoning_content``, OpenRouter's
``reasoning``, and plain OpenAI alike.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..config.schema import ModelDef, ProviderAccount
from ..constants import (
    ERROR_DETAIL_CHARS,
    HTTP_BAD_REQUEST,
    HTTP_FORBIDDEN,
    HTTP_NOT_FOUND,
    HTTP_OK,
    HTTP_TOO_MANY_REQUESTS,
    HTTP_UNAUTHORIZED,
    PROBE_TIMEOUT,
)
from . import proxy
from .base import (
    AuthError,
    CompletionRequest,
    Message,
    Provider,
    ProviderError,
    RateLimitError,
    ReasoningDelta,
    StreamDone,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolCallDelta,
    Usage,
)

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 522, 524}


class OpenAICompatProvider(Provider):
    def __init__(
        self,
        account: ProviderAccount,
        model: ModelDef,
        *,
        client: httpx.AsyncClient | None = None,
        context_window: int | None = None,
    ):
        self.account = account
        self.model = model
        self.context_window = model.context_for(context_window)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(account.timeout), **proxy.client_kwargs(account)
        )

    # -- request assembly -------------------------------------------------

    @property
    def _url(self) -> str:
        return self.account.base_url.rstrip("/") + "/chat/completions"

    def _headers(self) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        if self.account.api_key:
            h["Authorization"] = f"Bearer {self.account.api_key}"
        h.update(self.account.headers)
        return h

    def _body(self, req: CompletionRequest, stream: bool) -> dict[str, Any]:
        # Echo reasoning when any prior assistant turn had it — required by
        # DeepSeek thinking mode, and harmless for gateways that ignore the field.
        echo_reasoning = any(m.role == "assistant" and m.reasoning for m in req.messages)
        body: dict[str, Any] = {
            "model": self.model.model,
            "messages": [
                m.to_wire(include_reasoning=echo_reasoning) for m in req.messages
            ],
            "stream": stream,
        }
        if stream:
            # Most gateways need this to report usage on the final chunk.
            body["stream_options"] = {"include_usage": True}

        max_tokens = req.max_tokens or self.model.max_output_tokens
        if max_tokens:
            body["max_tokens"] = max_tokens

        if req.temperature is not None and self.model.supports_temperature:
            body["temperature"] = req.temperature

        if req.tools and self.model.supports_tools:
            body["tools"] = req.tools
            if req.tool_choice:
                body["tool_choice"] = req.tool_choice

        if req.stop:
            body["stop"] = req.stop

        # Precedence: account defaults < model defaults < per-request extras.
        for extra in (self.account.extra_body, self.model.extra_body, req.extra_body):
            for key, val in (extra or {}).items():
                if isinstance(val, dict) and isinstance(body.get(key), dict):
                    body[key] = {**body[key], **val}
                else:
                    body[key] = val
        return body

    # -- errors -----------------------------------------------------------

    def _raise_for_status(self, status: int, text: str) -> None:
        """Translate an HTTP error into the right :class:`ProviderError`.

        Raises:
          AuthError: On 401/403.
          RateLimitError: On 429.
          ProviderError: On anything else.
        """
        detail = text.strip()[:ERROR_DETAIL_CHARS]
        try:
            payload = json.loads(text)
            err = payload.get("error")
            if isinstance(err, dict):
                detail = err.get("message") or detail
            elif isinstance(err, str):
                detail = err
        except (json.JSONDecodeError, AttributeError):
            pass

        where = f"{self.account.id}/{self.model.id}"
        if status in (HTTP_UNAUTHORIZED, HTTP_FORBIDDEN):
            raise AuthError(f"[{where}] auth failed ({status}): {detail}")
        if status == HTTP_TOO_MANY_REQUESTS:
            raise RateLimitError(f"[{where}] rate limited: {detail}")
        raise ProviderError(
            f"[{where}] HTTP {status}: {detail}",
            status=status,
            retryable=status in RETRYABLE_STATUS,
        )

    # -- streaming --------------------------------------------------------

    async def stream(self, req: CompletionRequest) -> AsyncIterator[StreamEvent]:
        if not (req.stream and self.model.supports_streaming):
            yield await self._complete_once(req)
            return

        body = self._body(req, stream=True)
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_acc: dict[int, dict[str, str]] = {}
        usage = Usage()
        finish_reason = "stop"

        try:
            async with self._client.stream(
                "POST", self._url, headers=self._headers(), json=body,
                timeout=httpx.Timeout(self.account.timeout),
            ) as resp:
                if resp.status_code >= HTTP_BAD_REQUEST:
                    raw = (await resp.aread()).decode("utf-8", "replace")
                    self._raise_for_status(resp.status_code, raw)

                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    if chunk.get("usage"):
                        usage = _parse_usage(chunk["usage"])

                    # Some gateways stream an error object mid-body.
                    if chunk.get("error"):
                        err = chunk["error"]
                        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                        raise ProviderError(
                            f"[{self.account.id}/{self.model.id}] stream error: {msg}",
                            retryable=True,
                        )

                    for choice in chunk.get("choices") or []:
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
                        delta = choice.get("delta") or {}

                        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                        if reasoning:
                            reasoning_parts.append(reasoning)
                            yield ReasoningDelta(reasoning)

                        content = delta.get("content")
                        if content:
                            text_parts.append(content)
                            yield TextDelta(content)

                        for tc in delta.get("tool_calls") or []:
                            idx = tc.get("index", 0)
                            slot = tool_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] += fn["name"]
                            if fn.get("arguments"):
                                slot["arguments"] += fn["arguments"]
                            yield ToolCallDelta(
                                index=idx,
                                id=tc.get("id"),
                                name=fn.get("name"),
                                arguments=fn.get("arguments"),
                            )
        except httpx.TimeoutException as e:
            raise ProviderError(
                f"[{self.account.id}/{self.model.id}] timed out after {self.account.timeout}s",
                retryable=True,
            ) from e
        except httpx.HTTPError as e:
            raise ProviderError(
                f"[{self.account.id}/{self.model.id}] transport error: {e}", retryable=True
            ) from e

        message = Message(
            role="assistant",
            content="".join(text_parts),
            reasoning="".join(reasoning_parts),
            tool_calls=[
                ToolCall(
                    id=slot["id"] or f"call_{idx}",
                    name=slot["name"],
                    arguments=slot["arguments"],
                )
                for idx, slot in sorted(tool_acc.items())
                if slot["name"]
            ],
        )
        if message.tool_calls and finish_reason == "stop":
            finish_reason = "tool_calls"

        yield StreamDone(
            message=message,
            usage=usage,
            finish_reason=finish_reason,
            model=self.model.id,
            account=self.account.id,
        )

    # -- non-streaming ----------------------------------------------------

    async def _complete_once(self, req: CompletionRequest) -> StreamDone:
        body = self._body(req, stream=False)
        try:
            resp = await self._client.post(
                self._url,
                headers={**self._headers(), "Accept": "application/json"},
                json=body,
                timeout=httpx.Timeout(self.account.timeout),
            )
        except httpx.TimeoutException as e:
            raise ProviderError(
                f"[{self.account.id}/{self.model.id}] timed out", retryable=True
            ) from e
        except httpx.HTTPError as e:
            raise ProviderError(
                f"[{self.account.id}/{self.model.id}] transport error: {e}", retryable=True
            ) from e

        if resp.status_code >= HTTP_BAD_REQUEST:
            self._raise_for_status(resp.status_code, resp.text)

        payload = resp.json()
        choices = payload.get("choices") or []
        if not choices:
            raise ProviderError(f"[{self.account.id}] response had no choices")
        choice = choices[0]
        raw = choice.get("message") or {}

        message = Message(
            role="assistant",
            content=raw.get("content") or "",
            reasoning=raw.get("reasoning_content") or raw.get("reasoning") or "",
            tool_calls=[
                ToolCall(
                    id=tc.get("id") or f"call_{i}",
                    name=(tc.get("function") or {}).get("name", ""),
                    arguments=(tc.get("function") or {}).get("arguments", "") or "",
                )
                for i, tc in enumerate(raw.get("tool_calls") or [])
                if (tc.get("function") or {}).get("name")
            ],
        )
        return StreamDone(
            message=message,
            usage=_parse_usage(payload.get("usage") or {}),
            finish_reason=choice.get("finish_reason") or "stop",
            model=self.model.id,
            account=self.account.id,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _parse_usage(raw: dict[str, Any]) -> Usage:
    details = raw.get("prompt_tokens_details") or {}
    out_details = raw.get("completion_tokens_details") or {}
    return Usage(
        input_tokens=raw.get("prompt_tokens") or 0,
        output_tokens=raw.get("completion_tokens") or 0,
        cached_tokens=details.get("cached_tokens")
        or raw.get("prompt_cache_hit_tokens")
        or 0,
        reasoning_tokens=out_details.get("reasoning_tokens") or 0,
    )


async def probe_account(
    account: ProviderAccount, timeout: float = PROBE_TIMEOUT
) -> tuple[bool, str]:
    """Check whether an account's credentials work.

    Args:
      account: The account to probe.
      timeout: Seconds to wait for the endpoint.

    Returns:
      A tuple of (reachable, human-readable detail).
    """
    url = account.base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {account.api_key}"} if account.api_key else {}
    headers.update(account.headers)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as e:
        return False, f"unreachable: {e}"
    if resp.status_code == HTTP_OK:
        try:
            n = len(resp.json().get("data") or [])
            return True, f"ok ({n} models visible)"
        except (json.JSONDecodeError, AttributeError):
            return True, "ok"
    if resp.status_code in (HTTP_UNAUTHORIZED, HTTP_FORBIDDEN):
        return False, f"auth rejected ({resp.status_code})"
    # A 404 on /models is common on gateways that only expose /chat/completions.
    if resp.status_code == HTTP_NOT_FOUND:
        return True, "reachable (no /models endpoint)"
    return False, f"HTTP {resp.status_code}"
