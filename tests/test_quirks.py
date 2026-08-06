"""Recovering from parameters an endpoint refuses.

"OpenAI-compatible" is a family resemblance. Kimi's k3 rejects any
temperature other than 1, which killed the turn with a 400 and left the user
to work out which config field to edit. The turn matters more than the knob.
"""

from __future__ import annotations

import pytest

from aiharness.config.schema import ModelDef
from aiharness.providers import quirks
from aiharness.providers.base import Message

from .fake_openai import Reply


def model(**kwargs) -> ModelDef:
    return ModelDef(id="k3", model="k3", **kwargs)


# -- detection -------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "invalid temperature: only 1 is allowed for this model",
        "HTTP 400: invalid temperature: only 1 is allowed for this model",
        "temperature is not supported for this model",
        "Unsupported value: 'temperature' does not support 0.2",
        "temperature must be 1",
    ],
)
def test_a_temperature_rejection_is_recognised(message):
    assert quirks.detect_rejected_parameter(message) == "temperature"


def test_top_p_rejections_are_recognised():
    assert quirks.detect_rejected_parameter("invalid top_p") == "top_p"


@pytest.mark.parametrize(
    "message",
    [
        "context length exceeded",
        "rate limit exceeded",
        "the model reasons about temperature in physics problems",
        "your prompt mentions top_p somewhere",
        "",
    ],
)
def test_unrelated_errors_are_left_alone(message):
    """Guessing would turn a clear error into a mysterious behaviour change.

    Anything not attributable to a named parameter must surface as itself.
    """
    assert quirks.detect_rejected_parameter(message) is None


# -- applying it -----------------------------------------------------------


def test_disabling_reports_whether_it_changed_anything():
    m = model()
    assert quirks.disable_parameter(m, "temperature") is True
    assert m.supports_temperature is False
    # Second time round, dropping it is not what will fix the call.
    assert quirks.disable_parameter(m, "temperature") is False


def test_an_unknown_parameter_cannot_be_disabled():
    assert quirks.disable_parameter(model(), "reasoning_effort") is False


def test_the_explanation_names_the_model_and_the_parameter():
    text = quirks.explain(model(), "temperature")
    assert "k3" in text and "temperature" in text
    assert "自动保存" in quirks.explain(model(), "temperature", persisted=True)


# -- end to end through the router ----------------------------------------


async def test_a_rejected_temperature_is_dropped_and_the_turn_survives(fake, config):
    """The whole point: a 400 about a knob must not cost the user their turn."""
    from aiharness.config.loader import load_config
    from aiharness.providers.router import Router, Selection

    fake.push(
        Reply(status=400, error="invalid temperature: only 1 is allowed for this model"),
        Reply(text="recovered"),
    )
    router = Router(config)
    selection = Selection(model_id="fake")
    request = router.build_request(selection, [Message(role="user", content="hi")])
    assert request.temperature is not None, "the default is what provokes the 400"

    chunks = [event async for event in router.stream(selection, request)]
    assert chunks, "the retry produced nothing"
    assert config.model("fake").supports_temperature is False
    assert any("temperature" in n for n in router.notices), "the change must be visible"
    assert any("自动保存" in n for n in router.notices)
    # Next launch must not rediscover the same 400.
    reloaded = load_config()
    assert reloaded.model("fake").supports_temperature is False


async def test_an_unrelated_400_still_fails(fake, config):
    """Recovery must not degenerate into stripping fields until something works."""
    from aiharness.providers.base import ProviderError
    from aiharness.providers.router import Router, Selection

    fake.push(*[Reply(status=400, error="context length exceeded")] * 4)
    router = Router(config)
    selection = Selection(model_id="fake")
    request = router.build_request(selection, [Message(role="user", content="hi")])

    with pytest.raises(ProviderError):
        [event async for event in router.stream(selection, request)]
    assert config.model("fake").supports_temperature is True
