"""The TUI boots, renders and dispatches slash commands."""

from __future__ import annotations

import pytest

from aiharness.ui.app import HarnessApp
from aiharness.ui.commands import REGISTRY, completions, dispatch

from .fake_openai import Reply


@pytest.fixture
def app(config, workspace, sessions):
    return HarnessApp(config, workspace)


async def test_app_mounts_and_shows_a_status_line(app):
    async with app.run_test() as pilot:
        await pilot.pause()
        from aiharness.ui.widgets import StatusBar

        status = app.query_one(StatusBar)
        rendered = str(status.render())
        assert "fake" in rendered  # the model id
        assert "yolo" in rendered  # the permission mode
        assert "$" in rendered  # the cost readout


async def test_typing_a_prompt_streams_an_answer(app, fake):
    fake.push(Reply(text="hello from the fake model"))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#prompt").value = "say hi"
        await pilot.press("enter")
        await pilot.pause(delay=1.0)

        assistant = [
            m.content for m in app.agent.messages if m.role == "assistant"
        ]
        assert "hello from the fake model" in assistant


async def test_slash_commands_do_not_reach_the_model(app, fake):
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#prompt").value = "/models"
        await pilot.press("enter")
        await pilot.pause()
        assert fake.requests == []
        assert app.agent.messages == []


async def test_model_command_switches_the_pinned_account(app):
    async with app.run_test() as pilot:
        await pilot.pause()
        output = await dispatch(app, "/model fake@primary")
        assert "fake@primary" in output
        assert app.agent.selection.account_id == "primary"


async def test_model_command_rejects_an_unknown_account(app):
    async with app.run_test() as pilot:
        await pilot.pause()
        output = await dispatch(app, "/model fake@ghost")
        assert "unknown account" in output
        assert app.agent.selection.account_id is None


async def test_mode_command_changes_the_permission_engine(app):
    async with app.run_test() as pilot:
        await pilot.pause()
        await dispatch(app, "/mode ask")
        assert app.permissions.mode == "ask"
        output = await dispatch(app, "/mode nonsense")
        assert "must be one of" in output
        assert app.permissions.mode == "ask"


async def test_effort_and_context_commands(app):
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "high" in await dispatch(app, "/effort high")
        assert app.agent.selection.effort == "high"
        assert "Unknown effort" in await dispatch(app, "/effort extreme")

        await dispatch(app, "/context 4000")
        assert app.agent.context_window() == 4000


async def test_unknown_command_suggests_alternatives(app):
    async with app.run_test() as pilot:
        await pilot.pause()
        output = await dispatch(app, "/mdel")
        assert "Unknown command" in output


async def test_help_lists_every_registered_command(app):
    async with app.run_test() as pilot:
        await pilot.pause()
        output = await dispatch(app, "/help")
        for name in {entry.name for entry in REGISTRY.values()}:
            assert f"/{name}" in output


async def test_new_session_clears_the_transcript(app, fake):
    fake.push(Reply(text="first answer"))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#prompt").value = "hello"
        await pilot.press("enter")
        await pilot.pause(delay=1.0)
        assert app.agent.messages

        first_id = app.session.meta.id
        await dispatch(app, "/new")
        assert app.session.meta.id != first_id
        assert app.agent.messages == []


async def test_job_commands_persist_and_list(app, monkeypatch, tmp_path):
    from aiharness.scheduler.jobs import JobStore

    app.jobs = JobStore(tmp_path / "jobs.json")
    async with app.run_test() as pilot:
        await pilot.pause()
        output = await dispatch(
            app, "/job add deps | weekly mon,thu 09:30 | check dependencies"
        )
        assert "deps" in output
        listing = await dispatch(app, "/job")
        assert "deps" in listing
        assert len(app.jobs.all()) == 1


async def test_job_add_reports_a_bad_schedule(app, tmp_path):
    from aiharness.scheduler.jobs import JobStore

    app.jobs = JobStore(tmp_path / "jobs.json")
    async with app.run_test() as pilot:
        await pilot.pause()
        output = await dispatch(app, "/job add x | not a schedule | do things")
        assert "Schedule syntax" in output
        assert app.jobs.all() == []


def test_completions_match_a_partial_command():
    matches = completions("/mo", limit=5)
    names = [name for name, _ in matches]
    assert "/model" in names or "/models" in names
    assert completions("hello", limit=5) == []
