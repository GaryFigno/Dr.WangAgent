"""End-to-end agent loop behaviour."""

from __future__ import annotations

import asyncio

import pytest

from aiharness.agent.loop import Agent, Done, Notice, ToolEnd, ToolStart
from aiharness.agent.prompts import build_environment_note, build_system_prompt

from .fake_openai import Reply, tool_call


async def collect(agent: Agent, prompt: str) -> list:
    return [event async for event in agent.run(prompt)]


@pytest.mark.asyncio
async def test_tool_call_round_trip(fake, agent_parts, sessions):
    config, router, tools, permissions, workspace = agent_parts
    fake.push(
        Reply(tool_calls=[tool_call("Read", {"file_path": "hello.txt"})]),
        Reply(text="The file says: line one"),
    )
    session = sessions.create(workspace)
    agent = Agent(config, router, tools, permissions, workspace, session=session)

    events = await collect(agent, "what is in hello.txt?")

    started = [e for e in events if isinstance(e, ToolStart)]
    ended = [e for e in events if isinstance(e, ToolEnd)]
    assert [e.name for e in started] == ["Read"]
    assert not ended[0].result.is_error
    assert "line one" in ended[0].result.content

    done = [e for e in events if isinstance(e, Done)][-1]
    assert done.text == "The file says: line one"
    # user, assistant(tool call), tool result, assistant(answer)
    assert [m.role for m in agent.messages] == ["user", "assistant", "tool", "assistant"]
    await router.aclose()


@pytest.mark.asyncio
async def test_messages_are_persisted_as_they_happen(fake, agent_parts, sessions):
    config, router, tools, permissions, workspace = agent_parts
    fake.push(
        Reply(tool_calls=[tool_call("Read", {"file_path": "hello.txt"})]),
        Reply(text="done"),
    )
    session = sessions.create(workspace)
    agent = Agent(config, router, tools, permissions, workspace, session=session)
    await collect(agent, "read it")

    reloaded = sessions.open(session.meta.id)
    assert reloaded is not None
    assert [m.role for m in reloaded.full_history] == [
        "user", "assistant", "tool", "assistant"
    ]
    await router.aclose()


@pytest.mark.asyncio
async def test_unknown_tool_is_reported_back_not_raised(fake, agent_parts, sessions):
    config, router, tools, permissions, workspace = agent_parts
    fake.push(
        Reply(tool_calls=[tool_call("Teleport", {"destination": "mars"})]),
        Reply(text="sorry about that"),
    )
    agent = Agent(
        config, router, tools, permissions, workspace, session=sessions.create(workspace)
    )
    events = await collect(agent, "go")

    ended = [e for e in events if isinstance(e, ToolEnd)]
    assert ended[0].result.is_error
    assert "No tool named 'Teleport'" in ended[0].result.content
    await router.aclose()


@pytest.mark.asyncio
async def test_turn_limit_stops_a_runaway_loop(fake, agent_parts, sessions):
    config, router, tools, permissions, workspace = agent_parts
    config.max_agent_turns = 3
    # Always ask for another tool call: the loop must stop itself.
    fake.default = Reply(tool_calls=[tool_call("Read", {"file_path": "hello.txt"})])
    agent = Agent(
        config, router, tools, permissions, workspace, session=sessions.create(workspace)
    )
    events = await collect(agent, "loop forever")

    warnings = [e for e in events if isinstance(e, Notice) and e.level == "warn"]
    assert any("max_agent_turns" in w.text for w in warnings)
    await router.aclose()


@pytest.mark.asyncio
async def test_parallel_reads_all_execute(fake, agent_parts, sessions, workspace):
    config, router, tools, permissions, _ = agent_parts
    (workspace / "second.txt").write_text("second file\n", encoding="utf-8")
    fake.push(
        Reply(
            tool_calls=[
                tool_call("Read", {"file_path": "hello.txt"}, "call_a"),
                tool_call("Read", {"file_path": "second.txt"}, "call_b"),
            ]
        ),
        Reply(text="read both"),
    )
    agent = Agent(
        config, router, tools, permissions, workspace, session=sessions.create(workspace)
    )
    events = await collect(agent, "read both files")

    ended = [e for e in events if isinstance(e, ToolEnd)]
    assert len(ended) == 2
    assert all(not e.result.is_error for e in ended)
    await router.aclose()


@pytest.mark.asyncio
async def test_hung_parallel_tool_times_out_without_blocking_siblings(
    fake, agent_parts, sessions, workspace, monkeypatch
):
    """One stuck PARALLEL_SAFE call must not freeze the whole turn."""
    import asyncio

    from aiharness.agent import loop as loop_mod
    from aiharness.tools.base import ToolResult

    monkeypatch.setattr(loop_mod, "PARALLEL_TOOL_TIMEOUT", 0.05)
    config, router, tools, permissions, _ = agent_parts
    (workspace / "second.txt").write_text("second file\n", encoding="utf-8")
    fake.push(
        Reply(
            tool_calls=[
                tool_call("Read", {"file_path": "hello.txt"}, "call_a"),
                tool_call("Read", {"file_path": "second.txt"}, "call_b"),
            ]
        ),
        Reply(text="partial"),
    )

    original = Agent._invoke

    async def slow_or_fast(self, call):
        if call.id == "call_a":
            await asyncio.sleep(30)
            return ToolResult(content="late")
        return await original(self, call)

    monkeypatch.setattr(Agent, "_invoke", slow_or_fast)
    agent = Agent(
        config, router, tools, permissions, workspace, session=sessions.create(workspace)
    )
    events = await collect(agent, "read both")
    by_id = {e.call_id: e for e in events if isinstance(e, ToolEnd)}
    assert by_id["call_a"].result.is_error
    assert "timed out" in by_id["call_a"].result.content
    assert not by_id["call_b"].result.is_error
    assert "second file" in by_id["call_b"].result.content
    await router.aclose()


@pytest.mark.asyncio
async def test_edit_requires_a_prior_read(fake, agent_parts, sessions, workspace):
    config, router, tools, permissions, _ = agent_parts
    fake.push(
        Reply(
            tool_calls=[
                tool_call(
                    "Edit",
                    {
                        "file_path": "hello.txt",
                        "old_string": "line one",
                        "new_string": "line uno",
                    },
                )
            ]
        ),
        Reply(text="acknowledged"),
    )
    agent = Agent(
        config, router, tools, permissions, workspace, session=sessions.create(workspace)
    )
    events = await collect(agent, "edit it")

    ended = [e for e in events if isinstance(e, ToolEnd)]
    assert ended[0].result.is_error
    assert "before editing" in ended[0].result.content
    assert "line one" in (workspace / "hello.txt").read_text(encoding="utf-8")
    await router.aclose()


def test_system_prompt_is_stable_across_turns(workspace):
    """The cached prefix must not change, or every request misses the cache."""
    first = build_system_prompt(workspace, shell="posix")
    second = build_system_prompt(workspace, shell="posix")
    assert first == second
    # Volatile facts live outside the system prompt.
    assert "Date:" not in first
    assert "Date:" in build_environment_note(workspace)


def test_git_summary_tolerates_null_stdout(workspace, monkeypatch):
    """Windows CREATE_NO_WINDOW sometimes yields stdout=None; must not abort turn."""
    import subprocess

    from aiharness.agent import prompts as prompts_mod
    from aiharness.agent.prompts import _git_summary

    class _NullOut:
        stdout = None
        returncode = 0

    (workspace / ".git").mkdir()
    monkeypatch.setattr(prompts_mod.shutil, "which", lambda _name: "git")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _NullOut())
    assert _git_summary(workspace) == ""
    assert "Date:" in build_environment_note(workspace, query="hello")


@pytest.mark.asyncio
async def test_user_message_stamps_the_environment_note_once(fake, agent_parts, sessions):
    config, router, tools, permissions, workspace = agent_parts
    agent = Agent(
        config, router, tools, permissions, workspace, session=sessions.create(workspace)
    )
    agent.add_user_message("first")
    stored = agent.messages[0].content
    agent.add_user_message("second")
    # The first message is untouched by the second turn, so its bytes — and
    # therefore the cached prefix — stay identical.
    assert agent.messages[0].content == stored
    assert agent.messages[0].meta["user_text"] == "first"
    await router.aclose()


def test_steer_queues_mid_turn_guidance(agent_parts, sessions):
    config, router, tools, permissions, workspace = agent_parts
    session = sessions.create(workspace)
    agent = Agent(config, router, tools, permissions, workspace, session=session)
    assert agent.steer("中途插队") is True
    assert agent.steer("  ") is False
    assert agent._take_steering() == ["中途插队"]
    assert agent._take_steering() == []


@pytest.mark.asyncio
async def test_steer_injects_after_tool_round(fake, agent_parts, sessions):
    """Guidance queued during tools is injected before the next model call."""
    config, router, tools, permissions, workspace = agent_parts
    fake.push(
        Reply(tool_calls=[tool_call("Read", {"file_path": "hello.txt"})]),
        Reply(text="ok after steer"),
    )
    session = sessions.create(workspace)
    agent = Agent(config, router, tools, permissions, workspace, session=session)
    original = agent._run_tools

    async def run_tools_and_steer(calls):
        async for event in original(calls):
            yield event
        agent.steer("中途插队：先停一下")

    agent._run_tools = run_tools_and_steer  # type: ignore[method-assign]
    events = await collect(agent, "read hello")
    notices = [e.text for e in events if isinstance(e, Notice)]
    assert any("中途引导" in text for text in notices)
    guided = [
        m for m in agent.messages
        if m.role == "user" and "<user_guidance>" in str(m.content)
    ]
    assert guided and "中途插队" in str(guided[0].content)
    done = [e for e in events if isinstance(e, Done)][-1]
    assert done.text == "ok after steer"
    await router.aclose()


@pytest.mark.asyncio
async def test_hard_cancel_during_tool_stops_the_turn(fake, agent_parts, sessions):
    """CancelledError must not be swallowed into a tool error while work continues."""
    config, router, tools, permissions, workspace = agent_parts
    fake.push(Reply(tool_calls=[tool_call("Read", {"file_path": "hello.txt"})]))
    session = sessions.create(workspace)
    agent = Agent(config, router, tools, permissions, workspace, session=session)

    original = agent._invoke_guarded

    async def cancel_mid_tool(call):
        agent.interrupt()
        raise asyncio.CancelledError

    agent._invoke_guarded = cancel_mid_tool  # type: ignore[method-assign]
    # Prefetch may cancel mid-stream (Done interrupted) or raise from the tool path.
    try:
        events = await collect(agent, "read hello")
    except asyncio.CancelledError:
        events = []
    else:
        dones = [event for event in events if isinstance(event, Done)]
        assert dones and dones[-1].interrupted
    assert agent._cancel.is_set()
    agent._invoke_guarded = original  # type: ignore[method-assign]
    await router.aclose()


async def test_interrupt_after_tool_calls_seals_results(fake, agent_parts, sessions):
    """Cancel between assistant tool_calls and tool results must not 400 later."""
    from aiharness.providers.base import Message, ToolCall
    from aiharness.session.store import unanswered_tool_calls

    config, router, tools, permissions, workspace = agent_parts
    session = sessions.create(workspace)
    agent = Agent(config, router, tools, permissions, workspace, session=session)
    agent.add_user_message("go")
    agent._append(
        Message(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(id="c1", name="Read", arguments='{"file_path":"hello.txt"}'),
                ToolCall(id="c2", name="Grep", arguments='{"pattern":"x"}'),
            ],
        )
    )
    assert len(unanswered_tool_calls(agent.messages)) == 2
    sealed = agent.seal_unanswered_tool_calls()
    assert sealed == 2
    assert unanswered_tool_calls(agent.messages) == []
    assert [message.role for message in agent.messages] == [
        "user",
        "assistant",
        "tool",
        "tool",
    ]
    # A second seal is a no-op — important when finally + CancelledError both run.
    assert agent.seal_unanswered_tool_calls() == 0
    await router.aclose()


@pytest.mark.asyncio
async def test_run_heals_orphaned_tool_calls_before_calling_model(
    fake, agent_parts, sessions
):
    from aiharness.providers.base import Message, ToolCall

    config, router, tools, permissions, workspace = agent_parts
    session = sessions.create(workspace)
    agent = Agent(config, router, tools, permissions, workspace, session=session)
    agent.add_user_message("go")
    agent._append(
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="c1", name="Read", arguments="{}")],
        )
    )
    fake.push(Reply(text="ok after heal"))
    events = await collect(agent, "why did it stop?")
    assert [message.role for message in agent.messages[:4]] == [
        "user",
        "assistant",
        "tool",
        "user",
    ]
    assert any(isinstance(event, Done) and "heal" in event.text for event in events)
    await router.aclose()
