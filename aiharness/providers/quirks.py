"""Recovering from parameters an endpoint refuses.

"OpenAI-compatible" is a family resemblance, not a specification. Endpoints
differ on which knobs they accept, and several reasoning models reject the
ones an ordinary chat model requires — Kimi's ``k3`` answers a perfectly
well-formed request with::

    HTTP 400: invalid temperature: only 1 is allowed for this model

The knob is not important; the turn is. Rather than failing and leaving the
user to guess which field to edit in a config file, the request is retried
without the offending parameter and the model is marked so the mistake is
made once per session instead of once per call.

Only *unambiguous* rejections are matched. A 400 that cannot be attributed to
a specific parameter is left alone: silently dropping fields until a call
succeeds would turn a clear error into a mysterious change in behaviour.
"""

from __future__ import annotations

import re

from ..config.schema import ModelDef

#: Parameters this module knows how to give up on, and the attribute on
#: :class:`ModelDef` that records the model does not accept them.
SUPPORT_FLAGS = {
    "temperature": "supports_temperature",
    "top_p": "supports_temperature",
}

#: A rejection names the parameter and complains about its value. Both halves
#: are required: "temperature" appearing in prose is not a rejection.
_COMPLAINT = r"(?:invalid|unsupported|not support(?:ed)?|unexpected|only|must be|cannot)"
PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(rf"{_COMPLAINT}[^\n]{{0,40}}\btemperature\b", re.I), "temperature"),
    (re.compile(rf"\btemperature\b[^\n]{{0,40}}{_COMPLAINT}", re.I), "temperature"),
    (re.compile(rf"{_COMPLAINT}[^\n]{{0,40}}\btop_p\b", re.I), "top_p"),
    (re.compile(rf"\btop_p\b[^\n]{{0,40}}{_COMPLAINT}", re.I), "top_p"),
)


def detect_rejected_parameter(message: str) -> str | None:
    """Name the parameter an endpoint refused, if it can be identified.

    Args:
      message: The provider's error text.

    Returns:
      The parameter name, or None when the error is about something else.
    """
    for pattern, parameter in PATTERNS:
        if pattern.search(message):
            return parameter
    return None


def disable_parameter(model: ModelDef, parameter: str) -> bool:
    """Record that a model does not accept a parameter.

    Returns:
      True if this changed anything. False means the flag was already off, so
      dropping the parameter is not what will fix the call and the caller
      should let the error through rather than retrying forever.
    """
    flag = SUPPORT_FLAGS.get(parameter)
    if flag is None or not getattr(model, flag, False):
        return False
    setattr(model, flag, False)
    return True


def explain(model: ModelDef, parameter: str, *, persisted: bool = False) -> str:
    """A one-line note for the transcript, so the change is not invisible."""
    if persisted:
        return (
            f"{model.id} 不接受 {parameter}，已对该模型停用并重试，配置已自动保存。"
        )
    return (
        f"{model.id} 不接受 {parameter}，已对该模型停用并重试。"
        f"要永久生效，在设置里保存配置。"
    )
