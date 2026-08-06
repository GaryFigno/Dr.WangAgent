"""Technical indicators, in plain Python.

Each function returns a list the same length as its input, with ``None`` for
the leading positions where there is not yet enough history. That convention
matters: padding with zeros or dropping the head both misalign the result
against the bars, and misaligned indicators are the kind of bug that looks
like a trading insight.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

#: Conventional defaults. Named because a bare 12 in a MACD call is unreadable.
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
RSI_PERIOD = 14
BOLLINGER_PERIOD = 20
BOLLINGER_STDDEV = 2.0
ATR_PERIOD = 14
#: Moving averages drawn by default on a chart.
DEFAULT_MA_PERIODS = (5, 20, 60)

Number = float | None


def sma(values: Sequence[float], period: int) -> list[Number]:
    """Simple moving average."""
    if period <= 0:
        raise ValueError("period must be positive")
    out: list[Number] = [None] * len(values)
    total = 0.0
    for index, value in enumerate(values):
        total += value
        if index >= period:
            total -= values[index - period]
        if index >= period - 1:
            out[index] = total / period
    return out


def ema(values: Sequence[float], period: int) -> list[Number]:
    """Exponential moving average, seeded with the first SMA."""
    if period <= 0:
        raise ValueError("period must be positive")
    out: list[Number] = [None] * len(values)
    if len(values) < period:
        return out
    multiplier = 2.0 / (period + 1)
    current = sum(values[:period]) / period
    out[period - 1] = current
    for index in range(period, len(values)):
        current = (values[index] - current) * multiplier + current
        out[index] = current
    return out


def stddev(values: Sequence[float], period: int) -> list[Number]:
    """Rolling population standard deviation."""
    out: list[Number] = [None] * len(values)
    for index in range(period - 1, len(values)):
        window = values[index - period + 1 : index + 1]
        mean = sum(window) / period
        variance = sum((value - mean) ** 2 for value in window) / period
        out[index] = variance**0.5
    return out


@dataclass
class MACD:
    """MACD line, signal line, and the histogram between them."""

    macd: list[Number]
    signal: list[Number]
    histogram: list[Number]


def macd(
    values: Sequence[float],
    fast: int = MACD_FAST,
    slow: int = MACD_SLOW,
    signal_period: int = MACD_SIGNAL,
) -> MACD:
    """Moving average convergence/divergence."""
    fast_line = ema(values, fast)
    slow_line = ema(values, slow)
    difference: list[Number] = [
        (f - s) if (f is not None and s is not None) else None
        for f, s in zip(fast_line, slow_line, strict=True)
    ]

    # The signal line is an EMA of the MACD line, which only exists once both
    # source EMAs do, so it is computed over the dense tail and re-padded.
    dense = [value for value in difference if value is not None]
    dense_signal = ema(dense, signal_period)
    signal: list[Number] = [None] * len(difference)
    offset = len(difference) - len(dense)
    for index, value in enumerate(dense_signal):
        signal[offset + index] = value

    histogram: list[Number] = [
        (m - s) if (m is not None and s is not None) else None
        for m, s in zip(difference, signal, strict=True)
    ]
    return MACD(macd=difference, signal=signal, histogram=histogram)


def rsi(values: Sequence[float], period: int = RSI_PERIOD) -> list[Number]:
    """Relative strength index, using Wilder's smoothing."""
    out: list[Number] = [None] * len(values)
    if len(values) <= period:
        return out

    gains = losses = 0.0
    for index in range(1, period + 1):
        change = values[index] - values[index - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    average_gain, average_loss = gains / period, losses / period
    out[period] = _rsi_value(average_gain, average_loss)

    for index in range(period + 1, len(values)):
        change = values[index] - values[index - 1]
        average_gain = (average_gain * (period - 1) + max(change, 0.0)) / period
        average_loss = (average_loss * (period - 1) + max(-change, 0.0)) / period
        out[index] = _rsi_value(average_gain, average_loss)
    return out


def _rsi_value(average_gain: float, average_loss: float) -> float:
    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0
    strength = average_gain / average_loss
    return 100.0 - (100.0 / (1.0 + strength))


@dataclass
class Bollinger:
    """Bollinger bands: middle SMA with bands at N standard deviations."""

    middle: list[Number]
    upper: list[Number]
    lower: list[Number]


def bollinger(
    values: Sequence[float],
    period: int = BOLLINGER_PERIOD,
    deviations: float = BOLLINGER_STDDEV,
) -> Bollinger:
    """Bollinger bands."""
    middle = sma(values, period)
    spread = stddev(values, period)
    upper: list[Number] = []
    lower: list[Number] = []
    for centre, width in zip(middle, spread, strict=True):
        if centre is None or width is None:
            upper.append(None)
            lower.append(None)
        else:
            upper.append(centre + deviations * width)
            lower.append(centre - deviations * width)
    return Bollinger(middle=middle, upper=upper, lower=lower)


def true_range(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]
) -> list[float]:
    """Per-bar true range, which accounts for gaps between sessions."""
    out: list[float] = []
    for index in range(len(closes)):
        if index == 0:
            out.append(highs[index] - lows[index])
            continue
        previous = closes[index - 1]
        out.append(
            max(
                highs[index] - lows[index],
                abs(highs[index] - previous),
                abs(lows[index] - previous),
            )
        )
    return out


def atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = ATR_PERIOD,
) -> list[Number]:
    """Average true range."""
    ranges = true_range(highs, lows, closes)
    out: list[Number] = [None] * len(ranges)
    if len(ranges) < period:
        return out
    current = sum(ranges[:period]) / period
    out[period - 1] = current
    for index in range(period, len(ranges)):
        current = (current * (period - 1) + ranges[index]) / period
        out[index] = current
    return out


def volume_ratio(volumes: Sequence[float], period: int = 5) -> list[Number]:
    """Volume against its own recent average — the 量比 traders watch."""
    averages = sma(volumes, period)
    out: list[Number] = []
    for volume, average in zip(volumes, averages, strict=True):
        out.append(volume / average if average else None)
    return out


@dataclass
class IndicatorSet:
    """Everything computed for one series, aligned to its bars."""

    moving_averages: dict[int, list[Number]]
    macd: MACD
    rsi: list[Number]
    bollinger: Bollinger
    atr: list[Number]
    volume_ratio: list[Number]

    def latest(self) -> dict[str, float | None]:
        """The most recent value of each indicator, for a compact summary."""

        def last(series: list[Number]) -> float | None:
            return next((v for v in reversed(series) if v is not None), None)

        summary: dict[str, float | None] = {
            f"MA{period}": last(values) for period, values in self.moving_averages.items()
        }
        summary.update(
            {
                "MACD": last(self.macd.macd),
                "MACD_signal": last(self.macd.signal),
                "MACD_hist": last(self.macd.histogram),
                "RSI": last(self.rsi),
                "BOLL_upper": last(self.bollinger.upper),
                "BOLL_middle": last(self.bollinger.middle),
                "BOLL_lower": last(self.bollinger.lower),
                "ATR": last(self.atr),
                "volume_ratio": last(self.volume_ratio),
            }
        )
        return summary


def compute(series: object, periods: Sequence[int] = DEFAULT_MA_PERIODS) -> IndicatorSet:
    """Compute the standard indicator set for a bar series.

    Args:
      series: A :class:`~aiharness.market.bars.BarSeries`.
      periods: Moving-average lengths to compute.

    Returns:
      An :class:`IndicatorSet` aligned index-for-index with the bars.
    """
    closes = series.closes()  # type: ignore[attr-defined]
    highs = series.highs()  # type: ignore[attr-defined]
    lows = series.lows()  # type: ignore[attr-defined]
    volumes = series.volumes()  # type: ignore[attr-defined]
    return IndicatorSet(
        moving_averages={period: sma(closes, period) for period in periods},
        macd=macd(closes),
        rsi=rsi(closes),
        bollinger=bollinger(closes),
        atr=atr(highs, lows, closes),
        volume_ratio=volume_ratio(volumes),
    )
