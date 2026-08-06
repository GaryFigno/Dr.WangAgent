"""Provider adapter and router behaviour."""

from __future__ import annotations

import pytest

from aiharness.config.schema import ProviderAccount
from aiharness.providers.base import Message, StreamDone, TextDelta, ToolCall
from aiharness.providers.router import NoRouteError, Router, Selection

from .conftest import make_config
from .fake_openai import Reply, tool_call


@pytest.mark.asyncio
async def test_streams_text_and_reports_usage(config, router):
    selection = Selection(model_id="fake")
    request = router.build_request(selection, [Message(role="user", content="hi")])

    deltas: list[str] = []
    done: StreamDone | None = None
    async for event in router.stream(selection, request):
        if isinstance(event, TextDelta):
            deltas.append(event.text)
        elif isinstance(event, StreamDone):
            done = event

    assert done is not None
    assert "".join(deltas) == "ok"
    assert done.message.content == "ok"
    assert done.usage.input_tokens == 100
    assert done.usage.cached_tokens == 40
    await router.aclose()


@pytest.mark.asyncio
async def test_accumulates_split_tool_call_arguments(fake, config, router):
    fake.push(Reply(tool_calls=[tool_call("Read", {"file_path": "hello.txt"})]))
    selection = Selection(model_id="fake")
    request = router.build_request(selection, [Message(role="user", content="read it")])

    done = await router.complete(selection, request)
    assert len(done.message.tool_calls) == 1
    call = done.message.tool_calls[0]
    assert call.name == "Read"
    assert call.parsed() == {"file_path": "hello.txt"}
    await router.aclose()


@pytest.mark.asyncio
async def test_effort_is_translated_into_the_request_body(fake, config, router):
    selection = Selection(model_id="fake", effort="high")
    request = router.build_request(selection, [Message(role="user", content="hi")])
    await router.complete(selection, request)

    assert fake.requests[-1].body["reasoning_effort"] == "high"
    await router.aclose()


@pytest.mark.asyncio
async def test_pinned_account_is_used(fake, workspace):
    second = ProviderAccount(
        id="secondary", base_url=fake.base_url, api_key="key-secondary", priority=1
    )
    config = make_config(fake.base_url, extra_accounts=[second])
    router = Router(config)

    selection = Selection(model_id="fake", account_id="secondary")
    request = router.build_request(selection, [Message(role="user", content="hi")])
    done = await router.complete(selection, request)

    assert done.account == "secondary"
    assert fake.requests[-1].authorization == "Bearer key-secondary"
    await router.aclose()


@pytest.mark.asyncio
async def test_failover_moves_to_the_next_account(fake, workspace):
    second = ProviderAccount(
        id="secondary", base_url=fake.base_url, api_key="key-secondary", priority=20
    )
    config = make_config(fake.base_url, extra_accounts=[second])
    router = Router(config)

    # The primary is tried twice (retryable 500) before the secondary wins.
    fake.push(
        Reply(status=500, error="upstream exploded"),
        Reply(status=500, error="upstream exploded"),
        Reply(text="from the backup"),
    )
    selection = Selection(model_id="fake")
    request = router.build_request(selection, [Message(role="user", content="hi")])
    done = await router.complete(selection, request)

    assert done.message.content == "from the backup"
    assert done.account == "secondary"
    await router.aclose()


@pytest.mark.asyncio
async def test_auth_failure_is_not_retried_on_the_same_account(fake, workspace):
    second = ProviderAccount(
        id="secondary", base_url=fake.base_url, api_key="key-secondary", priority=20
    )
    config = make_config(fake.base_url, extra_accounts=[second])
    router = Router(config)

    fake.push(Reply(status=401, error="bad key"), Reply(text="secondary saved it"))
    selection = Selection(model_id="fake")
    request = router.build_request(selection, [Message(role="user", content="hi")])
    done = await router.complete(selection, request)

    assert done.account == "secondary"
    # One rejected call plus one successful call: no wasted retry on the 401.
    assert len(fake.requests) == 2
    await router.aclose()


@pytest.mark.asyncio
async def test_all_accounts_failing_raises(fake, config):
    router = Router(config)
    fake.push(*[Reply(status=500, error="down") for _ in range(4)])
    selection = Selection(model_id="fake")
    request = router.build_request(selection, [Message(role="user", content="hi")])

    with pytest.raises(NoRouteError):
        await router.complete(selection, request)
    await router.aclose()


def test_selection_parsing_rejects_unknown_pairs(config):
    assert Selection.parse("fake", config).model_id == "fake"
    assert Selection.parse("fake@primary", config).account_id == "primary"
    with pytest.raises(NoRouteError):
        Selection.parse("nope", config)
    with pytest.raises(NoRouteError):
        Selection.parse("fake@nope", config)


def test_malformed_tool_arguments_are_recovered():
    fenced = ToolCall(id="1", name="Read", arguments='```json\n{"file_path": "a.txt"}\n```')
    assert fenced.parsed() == {"file_path": "a.txt"}

    trailing = ToolCall(id="2", name="Read", arguments='Sure! {"file_path": "b.txt"}')
    assert trailing.parsed() == {"file_path": "b.txt"}

    with pytest.raises(ValueError):
        ToolCall(id="3", name="Read", arguments="not json at all").parsed()


def test_cost_accounting_prices_cached_tokens_separately(config):
    from aiharness.providers.base import Usage
    from aiharness.providers.router import compute_cost

    model = config.model("fake")
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000, cached_tokens=500_000)
    # 500k fresh input at $1, 500k cached at $0.10, 1M output at $2.
    assert compute_cost(model, usage) == pytest.approx(0.5 + 0.05 + 2.0)


def test_to_wire_echoes_reasoning_content_when_requested():
    """DeepSeek thinking mode requires prior reasoning_content on later turns."""
    plain = Message(role="assistant", content="answer", reasoning="chain")
    assert "reasoning_content" not in plain.to_wire()
    wired = plain.to_wire(include_reasoning=True)
    assert wired["reasoning_content"] == "chain"
    assert wired["content"] == "answer"


def test_openai_compat_body_echoes_prior_reasoning(config):
    from aiharness.providers.base import CompletionRequest
    from aiharness.providers.openai_compat import OpenAICompatProvider

    account = ProviderAccount(
        id="ds",
        base_url="https://example.invalid/v1",
        api_key="sk-test",
    )
    model = config.model("fake")
    assert model is not None
    provider = OpenAICompatProvider(account, model)
    body = provider._body(
        CompletionRequest(
            messages=[
                Message(role="user", content="hi"),
                Message(role="assistant", content="ok", reasoning="think"),
                Message(role="user", content="again"),
            ],
        ),
        stream=False,
    )
    assistant = body["messages"][1]
    assert assistant["reasoning_content"] == "think"
