"""Normalised message / streaming types shared by every provider."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str = ""  # raw JSON text as emitted by the model

    def parsed(self) -> dict[str, Any]:
        """Parse arguments, tolerating the malformed JSON weaker models emit."""
        text = (self.arguments or "").strip()
        if not text:
            return {}
        try:
            val = json.loads(text)
            return val if isinstance(val, dict) else {"value": val}
        except json.JSONDecodeError:
            pass
        # Some models wrap the object in markdown fences.
        if text.startswith("```"):
            body = text.split("```")[1] if "```" in text[3:] else text
            body = body.split("\n", 1)[-1] if body.startswith("json") else body
            try:
                val = json.loads(body)
                return val if isinstance(val, dict) else {"value": val}
            except json.JSONDecodeError:
                pass
        # Last resort: grab the outermost brace pair.
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                val = json.loads(text[start : end + 1])
                return val if isinstance(val, dict) else {"value": val}
            except json.JSONDecodeError:
                pass
        raise ValueError(f"tool call arguments are not valid JSON: {text[:200]}")


#: OpenAI multimodal content parts, or plain text.
MessageContent = str | list[dict[str, Any]]


def message_text(content: MessageContent) -> str:
    """Extract displayable text from a string or multimodal parts list."""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text":
            parts.append(str(part.get("text") or ""))
    return "\n".join(parts)


@dataclass
class Message:
    role: Role
    content: MessageContent = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    # Kept for display; echoed on the wire when ``include_reasoning`` is set
    # (DeepSeek thinking mode requires ``reasoning_content`` on later turns).
    reasoning: str = ""
    # Bookkeeping for context compaction.
    meta: dict[str, Any] = field(default_factory=dict)

    def to_wire(self, *, include_reasoning: bool = False) -> dict[str, Any]:
        """Serialise to the OpenAI chat-completions message shape."""
        msg: dict[str, Any] = {"role": self.role}
        if self.role == "tool":
            msg["content"] = self.content if isinstance(self.content, str) else message_text(
                self.content
            )
            msg["tool_call_id"] = self.tool_call_id or ""
            return msg
        # Multimodal user turns keep a parts list; assistants stay strings.
        if isinstance(self.content, list):
            msg["content"] = self.content
        else:
            # An assistant message with tool calls may legitimately have empty content.
            msg["content"] = self.content or ("" if self.tool_calls else self.content)
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments or "{}"},
                }
                for tc in self.tool_calls
            ]
        if self.name:
            msg["name"] = self.name
        # DeepSeek (and similar) reject the next call if prior thinking is omitted.
        if include_reasoning and self.role == "assistant" and self.reasoning:
            msg["reasoning_content"] = self.reasoning
        return msg


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cached_tokens + other.cached_tokens,
            self.reasoning_tokens + other.reasoning_tokens,
        )

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


# --------------------------------------------------------------------------
# streaming events
# --------------------------------------------------------------------------


@dataclass
class TextDelta:
    text: str


@dataclass
class ReasoningDelta:
    text: str


@dataclass
class ToolCallDelta:
    index: int
    id: str | None = None
    name: str | None = None
    arguments: str | None = None


@dataclass
class StreamDone:
    message: Message
    usage: Usage
    finish_reason: str = "stop"
    model: str = ""
    account: str = ""


StreamEvent = TextDelta | ReasoningDelta | ToolCallDelta | StreamDone


@dataclass
class CompletionRequest:
    messages: list[Message]
    tools: list[dict[str, Any]] = field(default_factory=list)
    max_tokens: int | None = None
    temperature: float | None = None
    stop: list[str] = field(default_factory=list)
    stream: bool = True
    # Merged last into the request body — effort params, vendor extras, etc.
    extra_body: dict[str, Any] = field(default_factory=dict)
    tool_choice: str | dict[str, Any] | None = None


class ProviderError(Exception):
    """Base for provider failures."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class RateLimitError(ProviderError):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message, status=429, retryable=True)
        self.retry_after = retry_after


class AuthError(ProviderError):
    def __init__(self, message: str):
        super().__init__(message, status=401, retryable=False)


class Provider:
    """Interface every backend adapter implements."""

    async def stream(self, req: CompletionRequest) -> AsyncIterator[StreamEvent]:
        raise NotImplementedError

    async def complete(self, req: CompletionRequest) -> StreamDone:
        """Non-streaming convenience wrapper built on top of stream()."""
        last: StreamDone | None = None
        async for event in self.stream(req):
            if isinstance(event, StreamDone):
                last = event
        if last is None:
            raise ProviderError("stream ended without a completion")
        return last

    async def aclose(self) -> None:
        pass
