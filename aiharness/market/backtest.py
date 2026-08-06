"""Tiny MA-cross backtest for the GUI (paper only, no live orders)."""

from __future__ import annotations

from dataclasses import dataclass

from .bars import BarSeries
from .indicators import sma


@dataclass
class BacktestResult:
    symbol: str
    fast: int
    slow: int
    trades: int
    return_pct: float
    max_drawdown_pct: float
    equity: list[dict[str, float | str]]

    def describe(self) -> str:
        return (
            f"{self.symbol} MA({self.fast}/{self.slow}) cross · "
            f"trades {self.trades} · return {self.return_pct:.2f}% · "
            f"max DD {self.max_drawdown_pct:.2f}%\n"
            f"（纸上回测 · 无真实下单）"
        )


def ma_cross_backtest(
    series: BarSeries, *, fast: int = 5, slow: int = 20
) -> BacktestResult:
    """Long when fast MA crosses above slow; flat otherwise. Close-to-close."""
    if fast < 1 or slow <= fast:
        raise ValueError("need 1 <= fast < slow")
    closes = [bar.close for bar in series.bars]
    days = [bar.day.isoformat() for bar in series.bars]
    fast_ma = sma(closes, fast)
    slow_ma = sma(closes, slow)
    cash = 1.0
    shares = 0.0
    peak = 1.0
    max_dd = 0.0
    trades = 0
    equity: list[dict[str, float | str]] = []
    prev_signal = 0
    for index, close in enumerate(closes):
        f_val = fast_ma[index]
        s_val = slow_ma[index]
        signal = 0
        if f_val is not None and s_val is not None:
            signal = 1 if f_val >= s_val else 0
        if signal == 1 and prev_signal == 0 and shares == 0:
            shares = cash / close
            cash = 0.0
            trades += 1
        elif signal == 0 and prev_signal == 1 and shares > 0:
            cash = shares * close
            shares = 0.0
            trades += 1
        prev_signal = signal
        value = cash + shares * close
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak)
        equity.append({"day": days[index], "equity": round(value, 6)})
    final = cash + shares * (closes[-1] if closes else 0.0)
    return BacktestResult(
        symbol=series.symbol,
        fast=fast,
        slow=slow,
        trades=trades,
        return_pct=(final - 1.0) * 100.0,
        max_drawdown_pct=max_dd * 100.0,
        equity=equity,
    )
