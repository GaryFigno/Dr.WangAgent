"""Compaction visibility, themes, preferences and the mascot."""

from __future__ import annotations

import time

import pytest

from aiharness.agent.loop import Compacted
from aiharness.providers.base import Message
from aiharness.session.store import CompactionRecord
from aiharness.ui.app import HarnessApp
from aiharness.ui.commands import dispatch
from aiharness.ui.mascot import Mascot, PetState
from aiharness.ui.prefs import UIPrefs
from aiharness.ui.theme import DEFAULT_THEME, THEMES, context_colour, get_theme
from aiharness.ui.widgets import CompactionDivider, context_gauge


@pytest.fixture
def app(config, workspace, sessions):
    return HarnessApp(config, workspace)


# -- the compaction marker -------------------------------------------------


def test_divider_states_what_compaction_cost():
    divider = CompactionDivider(
        "the story so far",
        tokens_before=47_231,
        tokens_after=12_880,
        replaced=40,
        chinese=False,
    )
    headline = str(divider._render_headline())
    assert "40 messages" in headline
    assert "47,231" in headline
    assert "12,880" in headline
    assert "34,351" in headline  # the saving, spelled out
    assert "/uncompact" in headline


async def test_divider_expands_to_the_full_note(app):
    summary = "a distinctive sentence. " * 60
    async with app.run_test() as pilot:
        await pilot.pause()
        app._handle_compacted(
            Compacted(summary, tokens_before=9000, tokens_after=900, replaced=8,
                      model="fake", automatic=True)
        )
        await pilot.pause()

        divider = app.query(CompactionDivider).first()
        collapsed = str(divider._render_detail())
        assert len(collapsed) < len(summary)  # only a preview at first

        divider.on_click()
        await pilot.pause()
        expanded = str(divider._render_detail())
        assert len(expanded) > len(collapsed)
        assert summary.strip() in expanded


async def test_compaction_event_puts_a_divider_in_the_transcript(app):
    async with app.run_test() as pilot:
        await pilot.pause()
        before = len(app.query(CompactionDivider))
        app._handle_compacted(
            Compacted(
                summary="what happened earlier",
                tokens_before=9000,
                tokens_after=2000,
                replaced=12,
                model="fake",
                automatic=True,
            )
        )
        await pilot.pause()
        assert len(app.query(CompactionDivider)) == before + 1


async def test_markers_can_be_switched_off(app):
    async with app.run_test() as pilot:
        await pilot.pause()
        await dispatch(app, "/markers off")
        app._handle_compacted(
            Compacted("s", tokens_before=9000, tokens_after=2000, replaced=3,
                      model="fake", automatic=True)
        )
        await pilot.pause()
        assert len(app.query(CompactionDivider)) == 0


async def test_resumed_session_replays_its_compaction_markers(config, workspace, sessions):
    session = sessions.create(workspace)
    for index in range(6):
        session.append(Message(role="user" if index % 2 == 0 else "assistant", content=str(index)))
    session.record_compaction(
        CompactionRecord(
            at=time.time(),
            replaced_through=4,
            summary="earlier turns",
            tokens_before=8000,
            tokens_after=900,
        )
    )

    app = HarnessApp(config, workspace, session_id=session.meta.id)
    async with app.run_test() as pilot:
        await pilot.pause()
        dividers = app.query(CompactionDivider)
        assert len(dividers) == 1
        assert "8,000" in str(dividers.first()._render_headline())


# -- context gauge ---------------------------------------------------------


def test_context_gauge_reports_percentage_and_fills():
    gauge = str(context_gauge(5000, 10000, width=10))
    assert "5,000/10,000" in gauge
    assert "50%" in gauge
    assert gauge.count("█") == 5


def test_context_colour_escalates_as_the_window_fills():
    calm = context_colour(0.1)
    warm = context_colour(0.8)
    hot = context_colour(0.97)
    assert calm != warm != hot
    assert hot == context_colour(1.0)


# -- themes ----------------------------------------------------------------


def test_every_theme_converts_to_a_textual_theme():
    for spec in THEMES.values():
        theme = spec.to_textual()
        assert theme.name == spec.name
        assert theme.primary


async def test_theme_command_switches_and_persists(app):
    async with app.run_test() as pilot:
        await pilot.pause()
        output = await dispatch(app, "/theme nord")
        assert "nord" in output
        assert app.theme == "nord"
        assert UIPrefs.load().theme == "nord"


async def test_theme_command_rejects_an_unknown_name(app):
    async with app.run_test() as pilot:
        await pilot.pause()
        output = await dispatch(app, "/theme chartreuse")
        assert "Unknown theme" in output
        assert app.theme == DEFAULT_THEME


async def test_theme_listing_marks_the_current_one(app):
    async with app.run_test() as pilot:
        await pilot.pause()
        output = await dispatch(app, "/theme")
        assert DEFAULT_THEME in output
        assert "←" in output


def test_get_theme_tolerates_separator_drift():
    assert get_theme("zhaocai_light") is not None
    assert get_theme("nope") is None


# -- mascot ----------------------------------------------------------------


def test_mascot_changes_face_but_keeps_its_shape():
    mascot = Mascot(chinese=False)
    idle = mascot.render()
    assert mascot.set_state(PetState.WORKING) is True
    working = mascot.render()
    assert idle != working
    # The silhouette lines are identical; only the face and caption move.
    assert idle.splitlines()[0] == working.splitlines()[0]
    assert idle.splitlines()[2] == working.splitlines()[2]


def test_mascot_reports_no_change_for_the_same_state():
    mascot = Mascot()
    mascot.set_state(PetState.WORKING)
    assert mascot.set_state(PetState.WORKING) is False


def test_mascot_emoji_style_is_one_line():
    mascot = Mascot(style="emoji")
    assert "\n" not in mascot.render()


def test_mascot_off_renders_nothing():
    assert Mascot(style="off").render() == ""


async def test_pet_command_toggles_and_persists(app):
    async with app.run_test() as pilot:
        await pilot.pause()
        await dispatch(app, "/pet off")
        assert UIPrefs.load().pet is False
        await dispatch(app, "/pet emoji")
        prefs = UIPrefs.load()
        assert prefs.pet is True
        assert prefs.pet_style == "emoji"


async def test_pet_follows_the_agent_state(app, fake):
    from .fake_openai import Reply

    fake.push(Reply(text="finished"))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#prompt").value = "do something"
        await pilot.press("enter")
        await pilot.pause(delay=1.0)
        assert app.mascot.state is PetState.HAPPY


# -- preferences -----------------------------------------------------------


def test_prefs_round_trip(tmp_path):
    path = tmp_path / "ui.json"
    prefs = UIPrefs(theme="matcha", pet=False, pet_style="emoji")
    prefs.save(path)
    assert UIPrefs.load(path).theme == "matcha"
    assert UIPrefs.load(path).pet is False


def test_corrupt_prefs_fall_back_to_defaults(tmp_path):
    path = tmp_path / "ui.json"
    path.write_text("{not json", encoding="utf-8")
    assert UIPrefs.load(path).theme == DEFAULT_THEME


def test_unknown_pref_keys_are_ignored(tmp_path):
    path = tmp_path / "ui.json"
    path.write_text('{"theme": "nord", "from_the_future": 1}', encoding="utf-8")
    assert UIPrefs.load(path).theme == "nord"
