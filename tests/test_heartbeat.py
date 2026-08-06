"""Automatic iteration: the caps, the verdict tokens, and reconnection."""

from __future__ import annotations

import asyncio

import pytest

from aiharness.agent.heartbeat import (
    BLOCKED_PHRASE,
    DONE_PHRASE,
    Heartbeat,
    HeartbeatLimits,
    HeartbeatState,
    StopReason,
    continuation_prompt,
    read_verdict,
)
from aiharness.ui.commands import parse_heartbeat_flags

#: Short enough to keep tests fast, long enough to be a real interval.
TICK = 0.02


def make_heartbeat(runner, cost=lambda: 0.0):
    """A heartbeat with the interval floor lowered so tests run fast.

    Production always uses the default floor; lowering it here is the only
    way to exercise several beats without sleeping for half a minute.
    """
    stops: list[StopReason] = []
    beat = Heartbeat(
        runner,
        cost,
        on_stop=lambda state, reason: stops.append(reason),
        min_interval=0.0,
    )
    return beat, stops


async def drain(beat: Heartbeat, timeout: float = 2.0) -> None:
    """Wait for the heartbeat to stop itself."""
    deadline = asyncio.get_running_loop().time() + timeout
    while beat.active and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(TICK / 2)
    beat.stop()


# -- verdict tokens --------------------------------------------------------


def test_the_model_can_declare_the_goal_met():
    assert read_verdict(f"I checked the tests. {DONE_PHRASE}") is StopReason.GOAL_REACHED


def test_the_model_can_ask_for_a_human():
    assert read_verdict(f"{BLOCKED_PHRASE} I need the staging password.") is StopReason.NEEDS_HUMAN


def test_ordinary_prose_is_not_a_verdict():
    assert read_verdict("I finished the task and everything works now.") is None
    assert read_verdict("done!") is None


def test_the_prompt_restates_the_goal_and_demands_evidence():
    state = HeartbeatState(goal="make the suite pass", limits=HeartbeatLimits())
    prompt = continuation_prompt(state)
    assert "make the suite pass" in prompt
    assert DONE_PHRASE in prompt
    assert BLOCKED_PHRASE in prompt
    # It must push back on unverified claims of success.
    assert "verified, not assumed" in prompt
    # And it must not invite the agent to stall.
    assert "do not ask me whether to continue" in prompt


# -- limits ----------------------------------------------------------------


def test_limits_reject_nonsense():
    """Negative caps, and turning every cap off, are both refused."""
    for bad in (
        HeartbeatLimits(max_iterations=-1),
        HeartbeatLimits(max_cost=-0.5),
        HeartbeatLimits(max_minutes=-1),
        HeartbeatLimits(max_iterations=0, max_cost=0, max_minutes=0),
    ):
        with pytest.raises(ValueError):
            bad.validate()


def test_one_cap_off_is_allowed_while_another_still_bounds_the_run():
    """Zero means "no cap here", which is fine as long as one remains.

    Somebody may not care how many rounds it takes as long as it stays under
    a dollar, so each cap is independently optional.
    """
    HeartbeatLimits(max_iterations=0, max_cost=1.0, max_minutes=0).validate()
    HeartbeatLimits(max_iterations=5, max_cost=0, max_minutes=0).validate()
    HeartbeatLimits(max_iterations=0, max_cost=0, max_minutes=30).validate()


def test_iteration_cap_is_detected():
    state = HeartbeatState(goal="g", limits=HeartbeatLimits(max_iterations=3))
    state.iterations = 3
    assert state.check_limits(0.0) is StopReason.MAX_ITERATIONS


def test_spend_cap_counts_only_what_the_loop_spent():
    state = HeartbeatState(
        goal="g", limits=HeartbeatLimits(max_cost=1.0), cost_at_start=10.0
    )
    assert state.check_limits(10.5) is None  # spent 0.50 inside the loop
    assert state.check_limits(11.0) is StopReason.MAX_COST


def test_deadline_is_detected():
    import time

    state = HeartbeatState(goal="g", limits=HeartbeatLimits(max_minutes=0.001))
    state.started_at = time.time() - 60
    assert state.check_limits(0.0) is StopReason.DEADLINE


def test_repeated_failures_stop_the_loop():
    from aiharness.constants import HEARTBEAT_MAX_CONSECUTIVE_ERRORS

    state = HeartbeatState(goal="g", limits=HeartbeatLimits())
    state.consecutive_errors = HEARTBEAT_MAX_CONSECUTIVE_ERRORS
    assert state.check_limits(0.0) is StopReason.TOO_MANY_ERRORS


# -- the loop --------------------------------------------------------------


async def test_it_stops_when_the_agent_declares_success():
    calls: list[str] = []

    async def runner(prompt: str) -> str:
        calls.append(prompt)
        return DONE_PHRASE if len(calls) >= 2 else "still working"

    beat, stops = make_heartbeat(runner)
    beat.start("do the thing", HeartbeatLimits(max_iterations=10), interval=TICK)
    await drain(beat)

    assert stops == [StopReason.GOAL_REACHED]
    assert len(calls) == 2


async def test_it_stops_at_the_iteration_cap():
    async def runner(prompt: str) -> str:
        return "not finished yet"

    beat, stops = make_heartbeat(runner)
    beat.start("grind forever", HeartbeatLimits(max_iterations=3), interval=TICK)
    await drain(beat)

    assert stops == [StopReason.MAX_ITERATIONS]
    assert beat.state.iterations == 3


async def test_it_stops_at_the_spend_cap():
    spend = 0.0

    async def runner(prompt: str) -> str:
        nonlocal spend
        spend += 0.5
        return "working"

    beat, stops = make_heartbeat(runner, cost=lambda: spend)
    beat.start("expensive", HeartbeatLimits(max_iterations=99, max_cost=1.0), interval=TICK)
    await drain(beat)

    assert stops == [StopReason.MAX_COST]
    assert beat.state.iterations <= 3  # stopped well before the iteration cap


async def test_a_dropped_connection_is_retried_not_fatal():
    """The reconnect path: a failed beat must not end the run."""
    attempts = 0

    async def flaky(prompt: str) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("gateway hiccup")
        return DONE_PHRASE

    beat, stops = make_heartbeat(flaky)
    beat.start("survive a drop", HeartbeatLimits(max_iterations=5), interval=TICK)
    await drain(beat, timeout=15.0)

    assert attempts >= 2
    assert stops == [StopReason.GOAL_REACHED]


async def test_a_second_heartbeat_is_refused():
    async def runner(prompt: str) -> str:
        return "working"

    beat, _ = make_heartbeat(runner)
    beat.start("first", HeartbeatLimits(max_iterations=99), interval=1.0)
    try:
        with pytest.raises(RuntimeError):
            beat.start("second")
    finally:
        beat.stop()


def test_an_empty_goal_is_refused():
    beat, _ = make_heartbeat(lambda prompt: None)
    with pytest.raises(ValueError):
        beat.start("   ")


async def test_the_interval_is_clamped_by_default():
    """The production floor must stop a runaway sub-second loop."""
    from aiharness.constants import HEARTBEAT_MAX_INTERVAL, HEARTBEAT_MIN_INTERVAL

    async def runner(prompt: str) -> str:
        return "working"

    beat = Heartbeat(runner, lambda: 0.0)  # default floor
    state = beat.start("g", HeartbeatLimits(), interval=0.001)
    assert state.interval == HEARTBEAT_MIN_INTERVAL
    beat.stop()

    state = beat.start("g", HeartbeatLimits(), interval=99999.0)
    assert state.interval == HEARTBEAT_MAX_INTERVAL
    beat.stop()


async def test_stopping_reports_once():
    async def runner(prompt: str) -> str:
        return "working"

    beat, stops = make_heartbeat(runner)
    beat.start("g", HeartbeatLimits(max_iterations=99), interval=1.0)
    beat.stop(StopReason.USER_STOPPED)
    beat.stop(StopReason.USER_STOPPED)  # idempotent
    assert stops == [StopReason.USER_STOPPED]


# -- command parsing -------------------------------------------------------


def test_flags_are_split_from_the_goal():
    goal, flags = parse_heartbeat_flags(
        "make the tests pass --iterations 20 --cost 2.5 --minutes 45"
    )
    assert goal == "make the tests pass"
    assert flags == {"iterations": 20.0, "cost": 2.5, "minutes": 45.0}


def test_a_goal_with_no_flags_survives_intact():
    goal, flags = parse_heartbeat_flags("refactor the auth layer end to end")
    assert goal == "refactor the auth layer end to end"
    assert flags == {}


# -- through the app -------------------------------------------------------


async def test_heartbeat_command_starts_and_stops(config, workspace, sessions):
    from aiharness.ui.app import HarnessApp
    from aiharness.ui.commands import dispatch

    app = HarnessApp(config, workspace)
    async with app.run_test() as pilot:
        await pilot.pause()
        output = await dispatch(app, "/heartbeat finish the migration --iterations 2 --interval 600")
        assert "Heartbeat started" in output
        assert app.heartbeat.active

        status = await dispatch(app, "/heartbeat")
        assert "finish the migration" in status

        await dispatch(app, "/heartbeat stop")
        assert not app.heartbeat.active


async def test_heartbeat_help_shows_the_caps(config, workspace, sessions):
    from aiharness.ui.app import HarnessApp
    from aiharness.ui.commands import dispatch

    app = HarnessApp(config, workspace)
    async with app.run_test() as pilot:
        await pilot.pause()
        output = await dispatch(app, "/heartbeat")
        for flag in ("--iterations", "--cost", "--minutes", "--interval"):
            assert flag in output
