"""Price series: the one shape every market source is normalised into.

Deliberately built on plain lists and floats rather than pandas or numpy.
The harness's selling point is that it starts in under a second and sits in
about sixty megabytes; pulling in the scientific stack to draw a candlestick
chart would undo that for every user, including the ones who never look at a
stock. The datasets involved — a few thousand daily bars — are small enough
that the standard library is genuinely fast enough.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

from ..constants import MIN_BARS_FOR_STATISTICS


def parse_day(text: str) -> date:
    """Parse a ``YYYY-MM-DD`` trading day."""
    return datetime.strptime(text.strip()[:10], "%Y-%m-%d").date()


@dataclass(frozen=True)
class Bar:
    """One OHLCV bar."""

    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float = 0.0

    @property
    def change(self) -> float:
        """Move from open to close, as a fraction."""
        return (self.close - self.open) / self.open if self.open else 0.0

    @property
    def rising(self) -> bool:
        return self.close >= self.open

    @property
    def range(self) -> float:
        return self.high - self.low


@dataclass
class BarSeries:
    """An ordered run of bars for one instrument.

    Prices from a qlib store are **backward-adjusted**: they are comparable
    across splits and dividends but are not what the stock printed on the
    day. Anything user-facing has to say so, or the numbers look wrong.
    """

    symbol: str
    bars: list[Bar] = field(default_factory=list)
    #: Where the data came from, for display and for trusting it.
    source: str = ""
    adjusted: bool = True
    name: str = ""

    def __len__(self) -> int:
        return len(self.bars)

    def __iter__(self) -> Iterator[Bar]:
        return iter(self.bars)

    def __getitem__(self, index: int) -> Bar:
        return self.bars[index]

    @property
    def empty(self) -> bool:
        return not self.bars

    @property
    def latest(self) -> Bar | None:
        return self.bars[-1] if self.bars else None

    @property
    def first(self) -> Bar | None:
        return self.bars[0] if self.bars else None

    # -- extraction -------------------------------------------------------

    def closes(self) -> list[float]:
        return [bar.close for bar in self.bars]

    def highs(self) -> list[float]:
        return [bar.high for bar in self.bars]

    def lows(self) -> list[float]:
        return [bar.low for bar in self.bars]

    def volumes(self) -> list[float]:
        return [bar.volume for bar in self.bars]

    def days(self) -> list[date]:
        return [bar.day for bar in self.bars]

    # -- slicing ----------------------------------------------------------

    def tail(self, count: int) -> BarSeries:
        """The most recent ``count`` bars."""
        return self._derive(self.bars[-count:] if count > 0 else [])

    def between(self, start: date | None, end: date | None) -> BarSeries:
        """Bars within an inclusive date range."""
        selected = [
            bar
            for bar in self.bars
            if (start is None or bar.day >= start) and (end is None or bar.day <= end)
        ]
        return self._derive(selected)

    def _derive(self, bars: list[Bar]) -> BarSeries:
        return BarSeries(
            symbol=self.symbol,
            bars=bars,
            source=self.source,
            adjusted=self.adjusted,
            name=self.name,
        )

    # -- summary ----------------------------------------------------------

    def period_return(self) -> float:
        """Total return across the series, as a fraction."""
        if len(self.bars) < MIN_BARS_FOR_STATISTICS or not self.bars[0].close:
            return 0.0
        return (self.bars[-1].close - self.bars[0].close) / self.bars[0].close

    def max_drawdown(self) -> float:
        """Largest peak-to-trough fall on closing prices, as a fraction."""
        peak = float("-inf")
        worst = 0.0
        for bar in self.bars:
            peak = max(peak, bar.close)
            if peak > 0:
                worst = min(worst, (bar.close - peak) / peak)
        return worst

    def describe(self) -> str:
        """A one-paragraph factual summary, with no interpretation."""
        if self.empty:
            return f"{self.symbol}: no data"
        first, last = self.bars[0], self.bars[-1]
        label = f"{self.symbol}" + (f" ({self.name})" if self.name else "")
        adjustment = "backward-adjusted" if self.adjusted else "raw"
        return (
            f"{label} — {len(self.bars)} bars, {first.day} to {last.day}, "
            f"{adjustment} prices from {self.source or 'unknown source'}\n"
            f"last close {last.close:.2f}, day change {last.change * 100:+.2f}%, "
            f"period return {self.period_return() * 100:+.2f}%, "
            f"max drawdown {self.max_drawdown() * 100:.2f}%"
        )


def _amount_at(amounts: Sequence[float] | None, index: int) -> float:
    """Turnover for one bar, defaulting to zero when absent or NaN."""
    if not amounts or index >= len(amounts):
        return 0.0
    value = amounts[index]
    return 0.0 if math.isnan(value) else float(value)


def build_series(
    symbol: str,
    days: Sequence[date],
    opens: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    volumes: Sequence[float],
    amounts: Sequence[float] | None = None,
    *,
    source: str = "",
    adjusted: bool = True,
    name: str = "",
) -> BarSeries:
    """Assemble a series from parallel columns, dropping incomplete rows.

    Real stores have gaps: a field can be shorter than the calendar, or hold
    a NaN on a suspended day. Rows that are not fully populated are dropped
    rather than filled, because an invented price is worse than a missing one.
    """
    count = min(len(days), len(opens), len(highs), len(lows), len(closes), len(volumes))
    bars: list[Bar] = []
    for index in range(count):
        values = (opens[index], highs[index], lows[index], closes[index])
        if any(math.isnan(value) or value <= 0 for value in values):
            continue
        volume = volumes[index]
        bars.append(
            Bar(
                day=days[index],
                open=float(opens[index]),
                high=float(highs[index]),
                low=float(lows[index]),
                close=float(closes[index]),
                volume=0.0 if math.isnan(volume) else float(volume),
                amount=_amount_at(amounts, index),
            )
        )
    return BarSeries(symbol=symbol, bars=bars, source=source, adjusted=adjusted, name=name)
