"""Candlestick rendering for a terminal.

One bar per column, drawn with block characters. Half-block glyphs give two
vertical cells of resolution per text row, which is what makes a 20-row chart
readable rather than a staircase.

Colours follow the Chinese convention — **red is up, green is down** — which
is the opposite of Western charts. Getting this backwards would silently
invert the meaning of every chart for the user this is being built for.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..constants import PRICE_LABEL_TENS, PRICE_LABEL_THOUSANDS
from .bars import BarSeries
from .indicators import Number

#: Up and down colours, Chinese convention.
COLOUR_UP = "#e24c4c"
COLOUR_DOWN = "#3fa85f"
COLOUR_FLAT = "#8a8a8a"
#: Moving-average overlay colours, in the order periods are given.
MA_COLOURS = ("#e8a05c", "#5aa9e6", "#b48ead")

#: Default chart geometry.
DEFAULT_HEIGHT = 18
DEFAULT_VOLUME_HEIGHT = 5
DEFAULT_WIDTH = 96
#: Columns reserved for the price axis labels.
AXIS_WIDTH = 10
#: Minimum rows a panel can be drawn in.
MIN_PANEL_HEIGHT = 3
#: Price labels drawn down the axis.
AXIS_TICKS = 5

BODY = "█"
WICK = "│"
HALF_UPPER = "▀"
HALF_LOWER = "▄"
DOJI = "─"


@dataclass
class Cell:
    """One character of the chart, with its colour."""

    char: str = " "
    colour: str = ""


class Canvas:
    """A grid of coloured cells that renders to Rich markup."""

    def __init__(self, width: int, height: int):
        self.width = width
        self.height = height
        self._cells = [[Cell() for _ in range(width)] for _ in range(height)]

    def put(self, row: int, column: int, char: str, colour: str = "") -> None:
        if 0 <= row < self.height and 0 <= column < self.width:
            self._cells[row][column] = Cell(char, colour)

    def to_markup(self) -> str:
        """Render to Rich markup, coalescing runs of one colour."""
        lines: list[str] = []
        for row in self._cells:
            parts: list[str] = []
            run: list[str] = []
            colour = None
            for cell in row:
                if cell.colour != colour:
                    parts.append(_wrap("".join(run), colour))
                    run, colour = [], cell.colour
                run.append(cell.char)
            parts.append(_wrap("".join(run), colour))
            lines.append("".join(parts))
        return "\n".join(lines)


def _wrap(text: str, colour: str | None) -> str:
    if not text:
        return ""
    escaped = text.replace("[", "\\[")
    return f"[{colour}]{escaped}[/]" if colour else escaped


@dataclass
class Scale:
    """Maps prices onto rows, with half-block sub-resolution."""

    low: float
    high: float
    rows: int

    @property
    def span(self) -> float:
        return max(self.high - self.low, 1e-9)

    def to_subrow(self, price: float) -> int:
        """Convert a price to a half-row index, 0 at the top."""
        fraction = (price - self.low) / self.span
        fraction = min(max(fraction, 0.0), 1.0)
        return int(round((1.0 - fraction) * (self.rows * 2 - 1)))


def _price_scale(series: BarSeries, rows: int, overlays: Sequence[list[Number]]) -> Scale:
    """Choose the visible price range, including any overlay lines."""
    low = min(bar.low for bar in series)
    high = max(bar.high for bar in series)
    for overlay in overlays:
        values = [value for value in overlay if value is not None]
        if values:
            low = min(low, *values)
            high = max(high, *values)
    padding = (high - low) * 0.04
    return Scale(low=low - padding, high=high + padding, rows=rows)


def _draw_candle(canvas: Canvas, column: int, bar, scale: Scale) -> None:
    """Draw one candle: wick from high to low, body from open to close."""
    colour = COLOUR_UP if bar.rising else COLOUR_DOWN
    top_body, bottom_body = max(bar.open, bar.close), min(bar.open, bar.close)

    high_sub, low_sub = scale.to_subrow(bar.high), scale.to_subrow(bar.low)
    body_top_sub, body_bottom_sub = scale.to_subrow(top_body), scale.to_subrow(bottom_body)

    for sub in range(high_sub, low_sub + 1):
        row = sub // 2
        inside_body = body_top_sub <= sub <= body_bottom_sub
        char = BODY if inside_body else WICK
        # A body only one half-row tall becomes a half block, not a full one.
        if inside_body and body_top_sub == body_bottom_sub:
            char = HALF_UPPER if sub % 2 == 0 else HALF_LOWER
        canvas.put(row, column, char, colour)

    if high_sub == low_sub:  # a completely flat bar still needs a mark
        canvas.put(high_sub // 2, column, DOJI, COLOUR_FLAT)


def _draw_overlay(
    canvas: Canvas, values: Sequence[Number], scale: Scale, colour: str, offset: int
) -> None:
    """Draw a moving-average line over the candles."""
    for index, value in enumerate(values):
        if value is None:
            continue
        row = scale.to_subrow(value) // 2
        column = offset + index
        if 0 <= column < canvas.width and canvas._cells[row][column].char == " ":
            canvas.put(row, column, "·", colour)


def _draw_volume(canvas: Canvas, series: BarSeries, rows: int, offset: int) -> None:
    """Draw the volume histogram beneath the price panel."""
    peak = max((bar.volume for bar in series), default=0.0)
    if peak <= 0:
        return
    for index, bar in enumerate(series):
        column = offset + index
        filled = (bar.volume / peak) * rows * 2
        full_rows = int(filled // 2)
        has_half = (filled - full_rows * 2) >= 1
        colour = COLOUR_UP if bar.rising else COLOUR_DOWN
        for step in range(full_rows):
            canvas.put(rows - 1 - step, column, BODY, colour)
        if has_half and full_rows < rows:
            canvas.put(rows - 1 - full_rows, column, HALF_LOWER, colour)


def _format_price(value: float) -> str:
    if value >= PRICE_LABEL_THOUSANDS:
        return f"{value:,.0f}"
    if value >= PRICE_LABEL_TENS:
        return f"{value:.2f}"
    return f"{value:.3f}"


def render(
    series: BarSeries,
    *,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    volume_height: int = DEFAULT_VOLUME_HEIGHT,
    moving_averages: dict[int, list[Number]] | None = None,
) -> str:
    """Render a bar series as a terminal candlestick chart.

    Args:
      series: Bars to draw. Only the last ``width - AXIS_WIDTH`` are shown.
      width: Total character width, including the price axis.
      height: Rows for the price panel.
      volume_height: Rows for the volume panel; 0 hides it.
      moving_averages: Period to values, overlaid on the price panel.

    Returns:
      Rich markup, ready for a ``Static`` widget or ``rich.print``.
    """
    if series.empty:
        return f"[dim]{series.symbol}: no data to chart[/]"

    plot_width = max(width - AXIS_WIDTH, 10)
    visible = series.tail(plot_width)
    height = max(height, MIN_PANEL_HEIGHT)

    overlays = moving_averages or {}
    # Overlays cover the full series; trim them to the visible window.
    trimmed = {
        period: values[-len(visible) :] for period, values in overlays.items()
    }
    scale = _price_scale(visible, height, list(trimmed.values()))

    price_canvas = Canvas(width, height)
    for index, bar in enumerate(visible):
        _draw_candle(price_canvas, AXIS_WIDTH + index, bar, scale)
    for order, (_, values) in enumerate(sorted(trimmed.items())):
        _draw_overlay(
            price_canvas, values, scale, MA_COLOURS[order % len(MA_COLOURS)], AXIS_WIDTH
        )

    for tick in range(AXIS_TICKS):
        row = int(tick * (height - 1) / max(AXIS_TICKS - 1, 1))
        price = scale.high - (row / max(height - 1, 1)) * (scale.high - scale.low)
        label = _format_price(price).rjust(AXIS_WIDTH - 1)
        for offset, char in enumerate(label):
            price_canvas.put(row, offset, char, "#7a7a7a")

    lines = [price_canvas.to_markup()]

    if volume_height > 0:
        volume_canvas = Canvas(width, volume_height)
        _draw_volume(volume_canvas, visible, volume_height, AXIS_WIDTH)
        label = "vol".rjust(AXIS_WIDTH - 1)
        for offset, char in enumerate(label):
            volume_canvas.put(0, offset, char, "#7a7a7a")
        lines.append(volume_canvas.to_markup())

    lines.append(_date_axis(visible, width))
    return "\n".join(lines)


def _date_axis(series: BarSeries, width: int) -> str:
    """A first/last date line under the chart."""
    if series.empty:
        return ""
    first, last = series.first.day, series.latest.day
    left = " " * AXIS_WIDTH + str(first)
    right = str(last)
    gap = max(width - len(left) - len(right), 1)
    return f"[#7a7a7a]{left}{' ' * gap}{right}[/]"


def summarise(series: BarSeries, indicators: object | None = None) -> str:
    """A compact text summary to accompany the chart.

    Facts only. Interpreting them is the model's job, and the user's call.
    """
    if series.empty:
        return f"{series.symbol}: no data"
    lines = [series.describe()]
    if indicators is not None:
        latest = indicators.latest()  # type: ignore[attr-defined]
        rendered = "  ".join(
            f"{name} {value:.2f}" for name, value in latest.items() if value is not None
        )
        if rendered:
            lines.append(rendered)
    return "\n".join(lines)
