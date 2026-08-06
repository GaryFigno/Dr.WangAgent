"""Session heartbeat: keep a session iterating toward a goal.

Two problems, one mechanism.

The first is that long work stalls whenever a turn ends. The agent finishes a
step, says something, and waits — even when the goal is plainly unmet and the
next step is obvious. A heartbeat nudges it forward on a timer so it keeps
going without a human typing "continue" forty times.

The second is that connections drop. A gateway hiccups mid-stream, the turn
dies, and the work is stranded even though the transcript is intact on disk.
The heartbeat notices the session went idle without finishing and resumes it.

**This is the most dangerous feature in the harness**, because it removes the
human from the loop on a spend-money-and-edit-files action. So it is bounded
four ways, all of them enforced here rather than requested of the model:

* a hard iteration cap,
* a hard spend cap in dollars,
* a wall-clock deadline,
* and a stop phrase the model can use to declare the goal met.

Whichever comes first wins. When any limit trips the heartbeat stops itself
and says why; it never quietly renews.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum

from ..constants import (
    HEARTBEAT_DEFAULT_INTERVAL,
    HEARTBEAT_MAX_CONSECUTIVE_ERRORS,
    HEARTBEAT_MAX_INTERVAL,
    HEARTBEAT_MIN_INTERVAL,
    HEARTBEAT_RECONNECT_BACKOFF,
    HEARTBEAT_RECONNECT_CEILING,
)

#: The model writes this when it believes the goal is met. Deliberately
#: distinctive so it cannot appear by accident in ordinary prose.
DONE_PHRASE = "HEARTBEAT_GOAL_REACHED"
#: And this when it is blocked and a human is genuinely needed.
BLOCKED_PHRASE = "HEARTBEAT_NEEDS_HUMAN"
#: A cap left at zero is switched off. Any one may be off; not all three.
NO_LIMIT = 0


class StopReason(Enum):
    """Why a heartbeat stopped. Every one is reported to the user."""

    GOAL_REACHED = "goal reached"
    NEEDS_HUMAN = "the agent asked for a human"
    MAX_ITERATIONS = "iteration cap"
    MAX_COST = "spend cap"
    DEADLINE = "time limit"
    TOO_MANY_ERRORS = "too many consecutive failures"
    USER_STOPPED = "stopped by the user"

    @property
    def label_zh(self) -> str:
        return {
            "goal reached": "目标达成",
            "the agent asked for a human": "需要人工介入",
            "iteration cap": "达到轮数上限",
            "spend cap": "达到花费上限",
            "time limit": "达到时间上限",
            "too many consecutive failures": "连续失败过多",
            "stopped by the user": "用户停止",
        }[self.value]


@dataclass
class HeartbeatLimits:
    """The four caps. All of them are hard.

    Defaults are deliberately small. Somebody who wants an agent grinding for
    an hour on fifty dollars should have to type those numbers themselves.
    """

    max_iterations: int = 10
    max_cost: float = 1.0
    max_minutes: float = 30.0

    def validate(self) -> None:
        """Raise if the caps cannot bound the run.

        A cap left at :data:`NO_LIMIT` is off, which is a legitimate choice
        for any single one of them — somebody may not care how many rounds it
        takes as long as it stays under a dollar. Turning off *all three* is
        not a choice, it is an unbounded agent spending real money, so it is
        rejected rather than confirmed.

        Raises:
          ValueError: If a cap is negative, or if none of them is set.
        """
        for name, value in (
            ("max_iterations", self.max_iterations),
            ("max_cost", self.max_cost),
            ("max_minutes", self.max_minutes),
        ):
            if value < NO_LIMIT:
                raise ValueError(f"{name} cannot be negative")
        if not any((self.max_iterations, self.max_cost, self.max_minutes)):
            raise ValueError("至少要设一个上限：轮数、花费或时间")

    def capped(self, value: float) -> bool:
        """Whether a limit is switched on."""
        return value > NO_LIMIT

    def describe(self, chinese: bool = False) -> str:
        rounds = f"{self.max_iterations} 轮" if chinese else f"{self.max_iterations} iterations"
        minutes = f"{self.max_minutes:.0f} 分钟" if chinese else f"{self.max_minutes:.0f} min"
        unlimited = "不限" if chinese else "unlimited"
        parts = [
            rounds if self.capped(self.max_iterations) else unlimited,
            f"${self.max_cost:.2f}" if self.capped(self.max_cost) else unlimited,
            minutes if self.capped(self.max_minutes) else unlimited,
        ]
        return " · ".join(parts)


@dataclass
class HeartbeatState:
    """Live progress of one heartbeat run."""

    goal: str
    limits: HeartbeatLimits
    interval: float = HEARTBEAT_DEFAULT_INTERVAL
    iterations: int = 0
    started_at: float = field(default_factory=time.time)
    cost_at_start: float = 0.0
    consecutive_errors: int = 0
    last_beat_at: float = 0.0
    stopped: StopReason | None = None

    @property
    def elapsed_minutes(self) -> float:
        return (time.time() - self.started_at) / 60.0

    def spent(self, current_cost: float) -> float:
        return max(current_cost - self.cost_at_start, 0.0)

    def remaining(self, current_cost: float) -> str:
        """A short "budget left" line for the status bar.

        A cap that is switched off reports what has been *used* instead of
        what is left, because "∞ left" tells nobody anything and the number
        people actually want to watch is the one that keeps climbing.
        """
        caps = self.limits
        rounds = (
            f"{caps.max_iterations - self.iterations} 轮"
            if caps.capped(caps.max_iterations)
            else f"第 {self.iterations + 1} 轮"
        )
        money = (
            f"${caps.max_cost - self.spent(current_cost):.2f}"
            if caps.capped(caps.max_cost)
            else f"已花 ${self.spent(current_cost):.2f}"
        )
        minutes = (
            f"{max(caps.max_minutes - self.elapsed_minutes, 0):.0f}m"
            if caps.capped(caps.max_minutes)
            else f"已跑 {self.elapsed_minutes:.0f}m"
        )
        return f"{rounds} · {money} · {minutes}"

    def check_limits(self, current_cost: float) -> StopReason | None:
        """Which cap, if any, has been reached."""
        if self.limits.capped(self.limits.max_iterations) and (
            self.iterations >= self.limits.max_iterations
        ):
            return StopReason.MAX_ITERATIONS
        if self.limits.capped(self.limits.max_cost) and (
            self.spent(current_cost) >= self.limits.max_cost
        ):
            return StopReason.MAX_COST
        if self.limits.capped(self.limits.max_minutes) and (
            self.elapsed_minutes >= self.limits.max_minutes
        ):
            return StopReason.DEADLINE
        if self.consecutive_errors >= HEARTBEAT_MAX_CONSECUTIVE_ERRORS:
            return StopReason.TOO_MANY_ERRORS
        return None


def read_verdict(text: str) -> StopReason | None:
    """Look for the model's own declaration that it is finished or stuck."""
    if DONE_PHRASE in text:
        return StopReason.GOAL_REACHED
    if BLOCKED_PHRASE in text:
        return StopReason.NEEDS_HUMAN
    return None


def continuation_prompt(state: HeartbeatState, chinese: bool = False) -> str:
    """The nudge sent on each beat.

    It restates the goal — the agent may have compacted away the original
    request — and it insists on evidence, because the most common failure of
    an auto-iterating agent is declaring victory it has not checked.
    """
    caps = state.limits
    rounds = (
        f"iteration {state.iterations + 1} of {caps.max_iterations}"
        if caps.capped(caps.max_iterations)
        else f"iteration {state.iterations + 1}, no round cap"
    )
    money = (
        f"${caps.max_cost - state.spent(0.0):.2f} of budget"
        if caps.capped(caps.max_cost)
        else "no spend cap"
    )
    minutes = (
        f"{max(caps.max_minutes - state.elapsed_minutes, 0):.0f} minutes"
        if caps.capped(caps.max_minutes)
        else "no time cap"
    )
    budget = f"{rounds}; {money} and {minutes} remain"
    return (
        f"[Automatic continuation — {budget}.]\n\n"
        f"The standing goal is:\n\n{state.goal}\n\n"
        f"Assess honestly where that goal actually stands, then take the next "
        f"concrete step toward it. Do not re-summarise what you have already "
        f"done, and do not ask me whether to continue — continuing is the "
        f"point of this loop.\n\n"
        f"When the goal is genuinely met — verified, not assumed — reply with "
        f"the exact token {DONE_PHRASE} and say what you checked to establish "
        f"it. If you are blocked on something only a human can resolve, reply "
        f"with {BLOCKED_PHRASE} and state precisely what you need. Do not use "
        f"either token for anything else."
    )


#: Called with (state, reason) when a heartbeat stops.
StopListener = Callable[[HeartbeatState, StopReason], None]
#: Runs one iteration; returns the agent's final text for the turn.
IterationRunner = Callable[[str], Awaitable[str]]


class Heartbeat:
    """Drives one session's automatic iteration."""

    def __init__(
        self,
        run_iteration: IterationRunner,
        current_cost: Callable[[], float],
        *,
        on_stop: StopListener | None = None,
        on_beat: Callable[[HeartbeatState], None] | None = None,
        min_interval: float = HEARTBEAT_MIN_INTERVAL,
    ):
        """Create a heartbeat driver.

        Args:
          run_iteration: Runs one turn and returns the agent's closing text.
          current_cost: Reads the session's running spend, in dollars.
          on_stop: Called once when the loop ends, with the reason.
          on_beat: Called at the start of each iteration.
          min_interval: Floor on the beat interval. The default stops a
            runaway loop from hammering the API; only tests lower it.
        """
        self._run_iteration = run_iteration
        self._current_cost = current_cost
        self._on_stop = on_stop
        self._on_beat = on_beat
        self._min_interval = max(min_interval, 0.0)
        self._task: asyncio.Task | None = None
        self.state: HeartbeatState | None = None

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(
        self,
        goal: str,
        limits: HeartbeatLimits | None = None,
        interval: float = HEARTBEAT_DEFAULT_INTERVAL,
    ) -> HeartbeatState:
        """Begin iterating toward ``goal``.

        Args:
          goal: What the agent should keep working toward.
          limits: The caps; defaults are intentionally small.
          interval: Seconds between beats, clamped to a sane range.

        Returns:
          The live :class:`HeartbeatState`.

        Raises:
          RuntimeError: If a heartbeat is already running.
          ValueError: If the goal is empty or a limit is nonsensical.
        """
        if self.active:
            raise RuntimeError("a heartbeat is already running; stop it first")
        if not goal.strip():
            raise ValueError("a heartbeat needs a goal to iterate toward")

        limits = limits or HeartbeatLimits()
        limits.validate()
        self.state = HeartbeatState(
            goal=goal.strip(),
            limits=limits,
            interval=min(max(interval, self._min_interval), HEARTBEAT_MAX_INTERVAL),
            cost_at_start=self._current_cost(),
        )
        self._task = asyncio.create_task(self._loop(), name="aih-heartbeat")
        return self.state

    def stop(self, reason: StopReason = StopReason.USER_STOPPED) -> None:
        """Stop the loop and report why."""
        state = self.state
        if state is not None and state.stopped is None:
            state.stopped = reason
            if self._on_stop:
                self._on_stop(state, reason)
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        state = self.state
        assert state is not None
        try:
            while True:
                await asyncio.sleep(state.interval)
                reason = state.check_limits(self._current_cost())
                if reason is not None:
                    self.stop(reason)
                    return
                if await self._beat(state):
                    return
        except asyncio.CancelledError:
            raise

    async def _beat(self, state: HeartbeatState) -> bool:
        """Run one iteration.

        Returns:
          True when the loop should end.
        """
        state.iterations += 1
        state.last_beat_at = time.time()
        if self._on_beat:
            self._on_beat(state)

        try:
            text = await self._run_iteration(continuation_prompt(state))
            state.consecutive_errors = 0
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a dropped connection is expected here
            # This is the reconnect path: back off and try again next beat
            # rather than abandoning work that is still intact on disk.
            state.consecutive_errors += 1
            backoff = min(
                HEARTBEAT_RECONNECT_BACKOFF * 2 ** (state.consecutive_errors - 1),
                HEARTBEAT_RECONNECT_CEILING,
            )
            await asyncio.sleep(backoff)
            return False

        verdict = read_verdict(text)
        if verdict is not None:
            self.stop(verdict)
            return True
        return False
