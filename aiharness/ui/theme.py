"""Colour themes.

Each theme is a small set of anchor colours; Textual derives the rest of the
palette (borders, muted text, hover states) from them. Keeping the set small
is deliberate — a theme that specifies forty colours is a theme nobody can
edit without breaking it.

The default, ``zhaocai``, takes its palette from the project's mascot: cream
paper, tabby brown, and the terracotta of the cat's scarf as the accent.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from textual.theme import Theme as TextualTheme


@dataclass(frozen=True)
class ThemeSpec:
    """One selectable colour scheme."""

    name: str
    label: str
    dark: bool
    primary: str
    secondary: str
    accent: str
    background: str
    surface: str
    panel: str
    foreground: str
    success: str
    warning: str
    error: str
    #: Extra CSS variables exposed to styles.tcss.
    variables: dict[str, str] = field(default_factory=dict)

    def to_textual(self) -> TextualTheme:
        return TextualTheme(
            name=self.name,
            primary=self.primary,
            secondary=self.secondary,
            accent=self.accent,
            background=self.background,
            surface=self.surface,
            panel=self.panel,
            foreground=self.foreground,
            success=self.success,
            warning=self.warning,
            error=self.error,
            dark=self.dark,
            variables=dict(self.variables),
        )


#: Colours used for the context-capacity bar as it fills, low to high.
CONTEXT_LEVEL_COLOURS = ("#6bbf7b", "#c9a227", "#d97a3d", "#c04a3c")
#: Fractions at which the capacity bar changes colour.
CONTEXT_LEVEL_THRESHOLDS = (0.5, 0.75, 0.9)


THEMES: dict[str, ThemeSpec] = {
    "zhaocai": ThemeSpec(
        name="zhaocai",
        label="招财 — 暖奶油 / 陶土红（默认）",
        dark=True,
        primary="#c96f4a",       # the scarf
        secondary="#8a6a4f",     # tabby brown
        accent="#e8a05c",
        background="#1c1917",
        surface="#241f1c",
        panel="#2e2723",
        foreground="#efe4d6",
        success="#7fae6a",
        warning="#d9a441",
        error="#c85a4c",
        variables={"context-bar-empty": "#3a322c"},
    ),
    "zhaocai-light": ThemeSpec(
        name="zhaocai-light",
        label="招财·浅色 — 纸白 / 陶土红",
        dark=False,
        primary="#b8562f",
        secondary="#8a6a4f",
        accent="#c96f4a",
        background="#faf4ea",
        surface="#f3ebdd",
        panel="#e9dfcd",
        foreground="#2c2521",
        success="#4f7d3c",
        warning="#a8761a",
        error="#a83a2c",
        variables={"context-bar-empty": "#ded2bf"},
    ),
    "midnight": ThemeSpec(
        name="midnight",
        label="午夜 — 深蓝 / 青",
        dark=True,
        primary="#4f8ef7",
        secondary="#3d5a80",
        accent="#61dafb",
        background="#0d1117",
        surface="#131922",
        panel="#1b232f",
        foreground="#d8e2ef",
        success="#4ec9a5",
        warning="#e2b93b",
        error="#f0564a",
        variables={"context-bar-empty": "#232c3a"},
    ),
    "nord": ThemeSpec(
        name="nord",
        label="Nord — 冷灰蓝",
        dark=True,
        primary="#88c0d0",
        secondary="#5e81ac",
        accent="#8fbcbb",
        background="#2e3440",
        surface="#343b48",
        panel="#3b4252",
        foreground="#e5e9f0",
        success="#a3be8c",
        warning="#ebcb8b",
        error="#bf616a",
        variables={"context-bar-empty": "#434c5e"},
    ),
    "matcha": ThemeSpec(
        name="matcha",
        label="抹茶 — 墨绿 / 米",
        dark=True,
        primary="#8fb573",
        secondary="#5c7a4a",
        accent="#c3d6a3",
        background="#161a14",
        surface="#1e241b",
        panel="#272e23",
        foreground="#e4ead9",
        success="#8fb573",
        warning="#d4b25e",
        error="#c26a5c",
        variables={"context-bar-empty": "#333c2d"},
    ),
    "mono": ThemeSpec(
        name="mono",
        label="灰阶 — 无彩色，最省眼",
        dark=True,
        primary="#a8a8a8",
        secondary="#7a7a7a",
        accent="#d0d0d0",
        background="#141414",
        surface="#1c1c1c",
        panel="#252525",
        foreground="#e0e0e0",
        success="#b0b0b0",
        warning="#c8c8c8",
        error="#e8e8e8",
        variables={"context-bar-empty": "#303030"},
    ),
}

DEFAULT_THEME = "zhaocai"


def theme_names() -> list[str]:
    return list(THEMES)


def get_theme(name: str) -> ThemeSpec | None:
    spec = THEMES.get(name)
    if spec is not None:
        return spec
    normalised = name.strip().lower().replace("_", "-")
    return next((t for t in THEMES.values() if t.name == normalised), None)


def context_colour(fraction: float) -> str:
    """Pick the capacity-bar colour for a given fill fraction.

    Args:
      fraction: How full the context window is, 0.0 to 1.0.

    Returns:
      A hex colour string that darkens toward red as the window fills.
    """
    for threshold, colour in zip(CONTEXT_LEVEL_THRESHOLDS, CONTEXT_LEVEL_COLOURS, strict=False):
        if fraction < threshold:
            return colour
    return CONTEXT_LEVEL_COLOURS[-1]
