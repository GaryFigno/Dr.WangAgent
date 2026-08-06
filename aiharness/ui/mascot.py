"""招财 — the mascot that sits in the corner and reflects what the agent is doing.

It is a status indicator wearing a cat costume. The silhouette never changes;
only the face does, so a state change reads at a glance without the eye having
to re-find the shape. Rendering happens on state transitions only, not on
every token, so it costs nothing while streaming.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import Enum


class PetState(Enum):
    """What the agent is doing, as the mascot understands it."""

    IDLE = "idle"
    THINKING = "thinking"
    WORKING = "working"
    HAPPY = "happy"
    WORRIED = "worried"
    COMPACTING = "compacting"
    SLEEPING = "sleeping"


@dataclass(frozen=True)
class Face:
    """One expression: the middle line of the drawing, plus a caption."""

    eyes: str
    caption_zh: str
    caption_en: str
    #: Rich style applied to the whole mascot in this state.
    style: str = ""


FACES: dict[PetState, tuple[Face, ...]] = {
    PetState.IDLE: (
        Face("·ω·", "在的", "here", ""),
        Face("・_・", "等着", "waiting", ""),
        Face("·﹏·", "闲着", "idle", ""),
    ),
    PetState.THINKING: (
        Face("・◡・", "想想", "thinking", "$accent"),
        Face("｡•ᴗ•｡", "琢磨中", "pondering", "$accent"),
    ),
    PetState.WORKING: (
        Face(">ω<", "干活", "working", "$primary"),
        Face("๑•̀ω•́", "在忙", "busy", "$primary"),
    ),
    PetState.HAPPY: (
        Face("^ω^", "搞定", "done", "$success"),
        Face("≧ω≦", "好啦", "all set", "$success"),
    ),
    PetState.WORRIED: (
        Face("•́ ︿", "出错了", "trouble", "$error"),
        Face("╥﹏╥", "翻车", "failed", "$error"),
    ),
    PetState.COMPACTING: (
        Face("-ω-", "整理记忆", "tidying up", "$warning"),
    ),
    PetState.SLEEPING: (
        Face("-  -", "睡了 z", "asleep z", "$text-disabled"),
    ),
}

#: The fixed silhouette: a round tabby in a scarf.
TOP_LINE = " ╱\\_/\\ "
SCARF_LINE = " ╰─≈─╯ "
#: How wide the drawing is, for layout.
PET_WIDTH = len(TOP_LINE)


class Mascot:
    """Tracks mascot state and renders it."""

    def __init__(self, *, chinese: bool = True, style: str = "cat"):
        self.chinese = chinese
        self.style = style
        self._state = PetState.IDLE
        self._face = FACES[PetState.IDLE][0]

    @property
    def state(self) -> PetState:
        return self._state

    def set_state(self, state: PetState) -> bool:
        """Move to a new state, choosing a fresh expression.

        Args:
          state: The state to move to.

        Returns:
          True when the state actually changed, so callers can skip repaints.
        """
        if state is self._state:
            return False
        self._state = state
        self._face = random.choice(FACES[state])
        return True

    def caption(self) -> str:
        return self._face.caption_zh if self.chinese else self._face.caption_en

    def render(self) -> str:
        """Return the mascot as plain text, ready for a Static widget."""
        if self.style == "off":
            return ""
        if self.style == "emoji":
            return f"{self._emoji()} {self.caption()}"
        middle = f"({self._face.eyes})".center(PET_WIDTH)
        return f"{TOP_LINE}\n{middle}\n{SCARF_LINE}\n{self.caption().center(PET_WIDTH)}"

    def rich_style(self) -> str:
        return self._face.style

    def _emoji(self) -> str:
        return {
            PetState.IDLE: "🐱",
            PetState.THINKING: "🤔",
            PetState.WORKING: "🐈",
            PetState.HAPPY: "😺",
            PetState.WORRIED: "🙀",
            PetState.COMPACTING: "😽",
            PetState.SLEEPING: "😴",
        }[self._state]
