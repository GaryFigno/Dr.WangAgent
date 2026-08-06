"""Session durability: nothing is lost until the user deletes it."""

from __future__ import annotations

import time

from aiharness.providers.base import Message, ToolCall
from aiharness.session.store import CompactionRecord, SessionStore


def make_messages(count: int) -> list[Message]:
    return [
        Message(role="user" if index % 2 == 0 else "assistant", content=f"message {index}")
        for index in range(count)
    ]


def test_messages_survive_a_reload(sessions: SessionStore, workspace):
    session = sessions.create(workspace)
    for message in make_messages(6):
        session.append(message)

    reloaded = sessions.open(session.meta.id)
    assert reloaded is not None
    assert [m.content for m in reloaded.full_history] == [f"message {i}" for i in range(6)]
    assert reloaded.meta.message_count == 6


def test_tool_calls_round_trip_through_storage(sessions, workspace):
    session = sessions.create(workspace)
    session.append(
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="c1", name="Read", arguments='{"file_path":"a"}')],
        )
    )
    session.append(Message(role="tool", content="contents", tool_call_id="c1", name="Read"))

    reloaded = sessions.open(session.meta.id)
    assert reloaded is not None
    assistant, tool = reloaded.full_history
    assert assistant.tool_calls[0].parsed() == {"file_path": "a"}
    assert tool.tool_call_id == "c1"


def test_compaction_shrinks_the_view_but_not_the_record(sessions, workspace):
    session = sessions.create(workspace)
    for message in make_messages(10):
        session.append(message)

    session.record_compaction(
        CompactionRecord(at=time.time(), replaced_through=8, summary="the story so far")
    )

    assert len(session.full_history) == 10  # record untouched
    view = session.view()
    assert len(view) == 4  # summary + ack + the last two messages
    assert "the story so far" in view[0].content
    assert view[-1].content == "message 9"


def test_uncompacting_restores_the_full_view(sessions, workspace):
    session = sessions.create(workspace)
    for message in make_messages(10):
        session.append(message)
    session.record_compaction(
        CompactionRecord(at=time.time(), replaced_through=8, summary="summary")
    )
    assert len(session.view()) == 4

    dropped = session.drop_compactions()
    assert dropped == 1
    assert len(session.view()) == 10


def test_view_never_starts_with_an_orphaned_tool_result(sessions, workspace):
    session = sessions.create(workspace)
    session.append(Message(role="user", content="go"))
    session.append(
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="c1", name="Read", arguments="{}")],
        )
    )
    session.append(Message(role="tool", content="result", tool_call_id="c1"))
    session.append(Message(role="assistant", content="done"))

    # Compacting through the assistant call would leave the tool result orphaned.
    session.record_compaction(
        CompactionRecord(at=time.time(), replaced_through=2, summary="s")
    )
    view = session.view()
    assert view[2].role != "tool"


def test_pair_tool_calls_repairs_interrupt_holes(sessions, workspace):
    """An interrupt mid-tools must not poison the next provider call."""
    from aiharness.session.store import pair_tool_calls, unanswered_tool_calls

    session = sessions.create(workspace)
    session.append(Message(role="user", content="go"))
    session.append(
        Message(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(id="Grep:67", name="Grep", arguments="{}"),
                ToolCall(id="Read:69", name="Read", arguments="{}"),
            ],
        )
    )
    # User typed again before anything sealed the pending calls — the bug
    # from the screenshot.
    session.append(Message(role="user", content="为什么刚刚对话直接停止了？"))

    pending = unanswered_tool_calls(session.full_history)
    assert {call.id for call in pending} == {"Grep:67", "Read:69"}

    wired = pair_tool_calls(session.full_history)
    assert [message.role for message in wired] == [
        "user",
        "assistant",
        "tool",
        "tool",
        "user",
    ]
    assert {message.tool_call_id for message in wired if message.role == "tool"} == {
        "Grep:67",
        "Read:69",
    }
    # The durable record stays append-only; repair is wire-side only here.
    assert [message.role for message in session.full_history] == [
        "user",
        "assistant",
        "user",
    ]


def test_pair_tool_calls_drops_misplaced_tool_results(sessions, workspace):
    """DeepSeek 400: tool after a user turn must not be sent as-is."""
    from aiharness.session.store import pair_tool_calls, unanswered_tool_calls

    session = sessions.create(workspace)
    session.append(Message(role="user", content="go"))
    session.append(
        Message(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(id="c1", name="Read", arguments="{}"),
                ToolCall(id="c2", name="Grep", arguments="{}"),
            ],
        )
    )
    session.append(
        Message(role="tool", content="one", tool_call_id="c1", name="Read")
    )
    session.append(Message(role="user", content="插队"))
    # Late / misplaced result — must not survive on the wire after the user.
    session.append(
        Message(role="tool", content="two", tool_call_id="c2", name="Grep")
    )

    pending = unanswered_tool_calls(session.full_history)
    assert {call.id for call in pending} == {"c2"}

    wired = pair_tool_calls(session.full_history)
    assert [message.role for message in wired] == [
        "user",
        "assistant",
        "tool",
        "tool",
        "user",
    ]
    tools = [message for message in wired if message.role == "tool"]
    assert [message.tool_call_id for message in tools] == ["c1", "c2"]
    assert tools[1].meta.get("synthetic") is True
    assert wired[-1].role == "user"
    assert wired[-1].content == "插队"


def test_clear_erases_messages_but_keeps_the_session(sessions, workspace):
    session = sessions.create(workspace)
    for message in make_messages(4):
        session.append(message)
    session_id = session.meta.id

    session.clear_messages()

    assert session.full_history == []
    reopened = sessions.open(session_id)
    assert reopened is not None
    assert reopened.full_history == []
    assert reopened.meta.message_count == 0


def test_delete_removes_the_session_entirely(sessions, workspace):
    session = sessions.create(workspace)
    session.append(Message(role="user", content="hi"))
    session_id = session.meta.id

    assert sessions.delete(session_id) is True
    assert sessions.open(session_id) is None
    assert sessions.delete(session_id) is False


def test_delete_all_is_scoped_to_the_workspace(sessions, workspace, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    sessions.create(workspace).append(Message(role="user", content="a"))
    sessions.create(workspace).append(Message(role="user", content="b"))
    sessions.create(other).append(Message(role="user", content="c"))

    removed = sessions.delete_all(workspace=workspace)

    assert removed == 2
    assert len(sessions.list(workspace=workspace)) == 0
    assert len(sessions.list(workspace=other)) == 1


def test_a_torn_line_does_not_lose_the_rest_of_the_log(sessions, workspace):
    session = sessions.create(workspace)
    for message in make_messages(3):
        session.append(message)
    with session.messages_path.open("a", encoding="utf-8") as handle:
        handle.write('{"role": "user", "content": "truncat')  # simulate a crash

    reloaded = sessions.open(session.meta.id)
    assert reloaded is not None
    assert len(reloaded.full_history) == 3


def test_title_is_derived_from_the_first_user_message(sessions, workspace):
    session = sessions.create(workspace)
    session.append(Message(role="user", content="Fix the login redirect bug"))
    assert session.meta.title == "Fix the login redirect bug"


def test_the_title_ignores_the_environment_note(sessions, workspace):
    """The note is stamped on for cache stability; it is not what was asked."""
    session = sessions.create(workspace)
    session.append(
        Message(
            role="user",
            content="<environment>\nDate: 2026-08-04\n</environment>\n\nFix the login bug",
            meta={"user_text": "Fix the login bug"},
        )
    )
    assert session.meta.title == "Fix the login bug"


def test_the_title_falls_back_to_content_when_unstamped(sessions, workspace):
    session = sessions.create(workspace)
    session.append(Message(role="user", content="a plain message"))
    assert session.meta.title == "a plain message"


def test_truncate_from_rewinds_the_log(sessions, workspace):
    session = sessions.create(workspace)
    for message in make_messages(6):
        session.append(message)

    removed = session.truncate_from(2)
    assert removed == 4
    assert [m.content for m in session.full_history] == ["message 0", "message 1"]

    reloaded = sessions.open(session.meta.id)
    assert reloaded is not None
    assert [m.content for m in reloaded.full_history] == ["message 0", "message 1"]


def test_list_keep_shows_an_empty_open_session(sessions, workspace):
    empty = sessions.create(workspace)
    listed = sessions.list(workspace=workspace, include_empty=False, keep=empty.meta.id)
    assert [meta.id for meta in listed] == [empty.meta.id]
