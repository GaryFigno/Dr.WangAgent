"""Choosing where price data comes from.

Two sources, with different jobs:

* **qlib store** — the local binary history AIQuant already maintains. Fast,
  offline, free, and *the same data the backtests ran on*. That last point is
  why it is the default: an analysis that disagrees with the backtest because
  it silently used a different data source is worse than no analysis.
* **akshare** — a live feed for today's quote and intraday bars, which a
  nightly-dumped store cannot have.

The rule is simple and stated to the model in every result: history comes
from qlib, today comes from akshare, and every series says which it was.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .bars import Bar, BarSeries, build_series
from .qlib_store import QlibStore, QlibStoreError

#: Bars fetched from the live source when no count is given.
DEFAULT_LIVE_BARS = 120
#: Seconds before a live quote is considered stale and refetched.
QUOTE_CACHE_SECONDS = 30.0


class MarketDataError(Exception):
    """Raised when no configured source can answer a request."""


@dataclass
class Quote:
    """A point-in-time snapshot, which a daily store cannot provide."""

    symbol: str
    price: float
    change: float = 0.0
    change_percent: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    previous_close: float = 0.0
    volume: float = 0.0
    amount: float = 0.0
    name: str = ""
    at: datetime = field(default_factory=datetime.now)
    source: str = ""

    def describe(self) -> str:
        label = f"{self.symbol}" + (f" {self.name}" if self.name else "")
        return (
            f"{label}  {self.price:.2f}  {self.change:+.2f} ({self.change_percent:+.2f}%)\n"
            f"open {self.open:.2f}  high {self.high:.2f}  low {self.low:.2f}  "
            f"prev close {self.previous_close:.2f}\n"
            f"volume {self.volume:,.0f}  amount {self.amount:,.0f}\n"
            f"as of {self.at:%Y-%m-%d %H:%M:%S} from {self.source} (raw, unadjusted)"
        )


# --------------------------------------------------------------------------
# akshare
# --------------------------------------------------------------------------


def _akshare():
    """Import akshare lazily.

    Raises:
      MarketDataError: If it is not installed.
    """
    try:
        import akshare
    except ImportError as error:
        raise MarketDataError(
            'live quotes need akshare. Install it with: pip install "aiharness[market]"'
        ) from error
    return akshare


def _to_akshare_symbol(symbol: str) -> str:
    """Convert ``SH600000`` to the bare ``600000`` akshare expects."""
    cleaned = symbol.strip().upper()
    for prefix in ("SH", "SZ", "BJ"):
        if cleaned.startswith(prefix):
            return cleaned[len(prefix) :]
    if "." in cleaned:
        return cleaned.split(".")[0]
    return cleaned


class LiveSource:
    """Live quotes and daily bars from akshare."""

    name = "akshare"

    def quote(self, symbol: str) -> Quote:
        """Fetch a current quote.

        Raises:
          MarketDataError: If akshare is missing or the symbol is unknown.
        """
        akshare = _akshare()
        code = _to_akshare_symbol(symbol)
        try:
            frame = akshare.stock_zh_a_spot_em()
            row = frame[frame["代码"] == code]
        except Exception as error:  # noqa: BLE001 - the feed fails many ways
            raise MarketDataError(f"akshare quote failed for {symbol}: {error}") from error
        if row.empty:
            raise MarketDataError(f"akshare has no quote for {symbol}")

        record = row.iloc[0]

        def number(column: str) -> float:
            try:
                value = float(record[column])
                return 0.0 if math.isnan(value) else value
            except (KeyError, TypeError, ValueError):
                return 0.0

        return Quote(
            symbol=symbol.upper(),
            name=str(record.get("名称", "")),
            price=number("最新价"),
            change=number("涨跌额"),
            change_percent=number("涨跌幅"),
            open=number("今开"),
            high=number("最高"),
            low=number("最低"),
            previous_close=number("昨收"),
            volume=number("成交量"),
            amount=number("成交额"),
            source=self.name,
        )

    def bars(self, symbol: str, count: int = DEFAULT_LIVE_BARS, adjust: str = "qfq") -> BarSeries:
        """Fetch recent daily bars.

        Args:
          symbol: Symbol in any accepted shape.
          count: How many recent bars to return.
          adjust: ``qfq`` forward-adjusted, ``hfq`` backward, ``""`` raw.

        Raises:
          MarketDataError: If the fetch fails.
        """
        akshare = _akshare()
        code = _to_akshare_symbol(symbol)
        try:
            frame = akshare.stock_zh_a_hist(
                symbol=code, period="daily", adjust=adjust
            )
        except Exception as error:  # noqa: BLE001
            raise MarketDataError(f"akshare history failed for {symbol}: {error}") from error
        if frame is None or frame.empty:
            raise MarketDataError(f"akshare returned no history for {symbol}")

        frame = frame.tail(count)
        days = [
            value.date() if hasattr(value, "date") else date.fromisoformat(str(value)[:10])
            for value in frame["日期"]
        ]
        return build_series(
            symbol.upper(),
            days,
            list(frame["开盘"]),
            list(frame["最高"]),
            list(frame["最低"]),
            list(frame["收盘"]),
            list(frame["成交量"]),
            list(frame["成交额"]) if "成交额" in frame else None,
            source=f"akshare({adjust or 'raw'})",
            adjusted=bool(adjust),
        )


# --------------------------------------------------------------------------
# the router
# --------------------------------------------------------------------------


class MarketRouter:
    """Unified access to history and live quotes."""

    def __init__(self, store_path: str | Path | None = None, *, allow_live: bool = True):
        self.store = QlibStore(store_path) if store_path else None
        self.live = LiveSource() if allow_live else None
        self._quotes: dict[str, tuple[float, Quote]] = {}

    # -- capability -------------------------------------------------------

    @property
    def has_store(self) -> bool:
        return self.store is not None and self.store.available

    def describe_sources(self) -> str:
        lines = []
        if self.store is not None:
            state = "ready" if self.store.available else "not found"
            lines.append(f"- qlib store: {self.store.root} ({state})")
        else:
            lines.append("- qlib store: not configured")
        lines.append(
            "- live quotes: akshare" if self.live else "- live quotes: disabled"
        )
        return "\n".join(lines)

    # -- history ----------------------------------------------------------

    def history(
        self, symbol: str, count: int | None = None, *, prefer_live: bool = False
    ) -> BarSeries:
        """Load daily bars, preferring the local store.

        Args:
          symbol: Any accepted symbol shape.
          count: Most recent bars to return; all of them when omitted.
          prefer_live: Fetch from akshare even when the store has the symbol.

        Returns:
          A bar series tagged with the source it came from.

        Raises:
          MarketDataError: If no source could supply the symbol.
        """
        problems: list[str] = []

        if not prefer_live and self.has_store:
            try:
                series = self.store.bars(symbol)
                return series.tail(count) if count else series
            except QlibStoreError as error:
                problems.append(str(error))

        if self.live is not None:
            try:
                return self.live.bars(symbol, count or DEFAULT_LIVE_BARS)
            except MarketDataError as error:
                problems.append(str(error))

        if not prefer_live:
            raise MarketDataError(
                f"no source could supply {symbol}:\n  " + "\n  ".join(problems)
            )
        # prefer_live failed; fall back to the store rather than giving up.
        if self.has_store:
            try:
                series = self.store.bars(symbol)
                return series.tail(count) if count else series
            except QlibStoreError as error:
                problems.append(str(error))
        raise MarketDataError(
            f"no source could supply {symbol}:\n  " + "\n  ".join(problems)
        )

    # -- quotes -----------------------------------------------------------

    def quote(self, symbol: str, *, max_age: float = QUOTE_CACHE_SECONDS) -> Quote:
        """Fetch a live quote, briefly cached.

        The cache exists because a screen over thirty symbols would otherwise
        pull the whole market snapshot thirty times.

        Raises:
          MarketDataError: If no live source is available.
        """
        import time

        if self.live is None:
            raise MarketDataError("live quotes are disabled; only history is available")
        key = symbol.upper()
        cached = self._quotes.get(key)
        if cached and (time.time() - cached[0]) < max_age:
            return cached[1]
        quote = self.live.quote(symbol)
        self._quotes[key] = (time.time(), quote)
        return quote

    def latest_bar(self, symbol: str) -> Bar | None:
        """The most recent bar from history, without touching the network."""
        try:
            series = self.history(symbol, count=1)
        except MarketDataError:
            return None
        return series.latest

    # -- discovery --------------------------------------------------------

    def search(self, query: str, limit: int = 20) -> list[str]:
        """Find symbols in the local store."""
        if not self.has_store:
            return []
        return self.store.search(query, limit)
