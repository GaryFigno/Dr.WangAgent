"""Market tools: charts, quotes, screening and a paper account.

**There is no order-execution tool here, and that is deliberate.**

A language model reading a candlestick chart and a backtested quantitative
signal are not comparable sources of confidence, and only one of them has
been measured. Wiring the first to real money would be betting capital on
whether the model happened to describe the chart correctly this time. The
paper account exists so that bet can be settled with a month of logged trades
instead of a month of losses.

If the agent's paper record turns out to be good, placing the orders is still
the user's job — a person who can see the position, the size and the market
at the moment it happens.

Everything here reads from the same qlib store the backtests use, so an
analysis and a backtest cannot silently disagree about the data.
"""

from __future__ import annotations

from typing import Any

from ..constants import (
    MARKET_CHART_BARS,
    MARKET_CHART_HEIGHT,
    MARKET_CHART_WIDTH,
    MARKET_HISTORY_ROWS,
    MARKET_SCREEN_LIMIT,
    MARKET_VOLUME_HEIGHT,
)
from ..market import chart as chart_module
from ..market import indicators as indicator_module
from ..market.bars import BarSeries
from ..market.paper import PaperTradeError
from ..market.router import MarketDataError
from .base import Tool, ToolContext, ToolResult

#: A-share lot size, quoted in the PaperTrade description.
LOT_SIZE_HINT = 100

#: Appended to every analytical result. The model is good at forgetting this.
DATA_CAVEAT = (
    "\n\n_Historical prices are backward-adjusted, so they are comparable "
    "across splits but are not the printed price. Indicators describe what "
    "happened; they do not predict what will happen._"
)


def _router(ctx: ToolContext):
    """Get the session's market router.

    Raises:
      MarketDataError: If market access is not configured.
    """
    router = getattr(ctx, "market", None)
    if router is None:
        raise MarketDataError(
            "market data is not configured. Set `market: {enabled: true, "
            "qlib_store: <path>}` in your config."
        )
    return router


class MarketChartTool(Tool):
    """Renders a candlestick chart and the standard indicator set."""

    name = "MarketChart"
    bulky = True
    description = f"""
Draw a candlestick chart with moving averages, volume, and the usual
indicators (MACD, RSI, Bollinger, ATR, volume ratio).

This is the tool to reach for when asked to "look at" a stock. It returns
both the chart and the numbers, so describe what is actually there — the
trend, where price sits against the bands and averages, what volume did —
rather than reciting the indicator values back.

Defaults to {MARKET_CHART_BARS} daily bars from the local store.
"""

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "SH600519, 600519, 600519.SH or sz000001 all work",
                },
                "bars": {"type": "integer", "description": f"Bars to show, default {MARKET_CHART_BARS}"},
                "live": {
                    "type": "boolean",
                    "description": "Prefer the live feed over the local store",
                },
                "ma": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Moving-average periods, default [5, 20, 60]",
                },
            },
            "required": ["symbol"],
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        symbol = str(args.get("symbol", "")).strip()
        if not symbol:
            return ToolResult.error("no symbol given")
        count = int(args.get("bars") or MARKET_CHART_BARS)
        periods = [int(p) for p in (args.get("ma") or indicator_module.DEFAULT_MA_PERIODS)]

        try:
            router = _router(ctx)
            series = router.history(symbol, count=count, prefer_live=bool(args.get("live")))
        except MarketDataError as error:
            return ToolResult.error(str(error))
        if series.empty:
            return ToolResult.error(f"no bars available for {symbol}")

        indicators = indicator_module.compute(series, periods)
        drawing = chart_module.render(
            series,
            width=MARKET_CHART_WIDTH,
            height=MARKET_CHART_HEIGHT,
            volume_height=MARKET_VOLUME_HEIGHT,
            moving_averages=indicators.moving_averages,
        )
        summary = chart_module.summarise(series, indicators)
        return ToolResult(
            content=f"{summary}\n\n{drawing}{DATA_CAVEAT}",
            summary=f"charted {series.symbol}, {len(series)} bars",
            display={"kind": "chart", "symbol": series.symbol, "markup": drawing},
        )


class MarketQuoteTool(Tool):
    name = "MarketQuote"
    description = """
Fetch the current quote for a symbol from the live feed.

Use for "what is it at now". For anything historical use MarketChart or
MarketHistory, which read the local store and do not need the network.
"""

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            quote = _router(ctx).quote(str(args.get("symbol", "")))
        except MarketDataError as error:
            return ToolResult.error(str(error))
        return ToolResult(
            content=quote.describe(),
            summary=f"{quote.symbol} {quote.price:.2f} ({quote.change_percent:+.2f}%)",
        )


class MarketHistoryTool(Tool):
    name = "MarketHistory"
    bulky = True
    description = f"""
Return raw OHLCV rows as a table.

Use when you need the actual numbers — to compute something yourself, or to
check a specific date. For reading a trend, MarketChart is easier to
interpret. Capped at {MARKET_HISTORY_ROWS} rows.
"""

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "count": {"type": "integer"},
                "start": {"type": "string", "description": "YYYY-MM-DD"},
                "end": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["symbol"],
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        from ..market.bars import parse_day

        try:
            series = _router(ctx).history(str(args.get("symbol", "")))
        except MarketDataError as error:
            return ToolResult.error(str(error))

        if args.get("start") or args.get("end"):
            try:
                start = parse_day(str(args["start"])) if args.get("start") else None
                end = parse_day(str(args["end"])) if args.get("end") else None
            except ValueError as error:
                return ToolResult.error(f"bad date: {error}")
            series = series.between(start, end)

        series = series.tail(min(int(args.get("count") or MARKET_HISTORY_ROWS), MARKET_HISTORY_ROWS))
        if series.empty:
            return ToolResult.error("no rows in that range")

        header = f"{'date':<12}{'open':>10}{'high':>10}{'low':>10}{'close':>10}{'volume':>14}"
        rows = [
            f"{str(bar.day):<12}{bar.open:>10.2f}{bar.high:>10.2f}"
            f"{bar.low:>10.2f}{bar.close:>10.2f}{bar.volume:>14,.0f}"
            for bar in series
        ]
        return ToolResult(
            content=f"{series.describe()}\n\n{header}\n" + "\n".join(rows) + DATA_CAVEAT,
            summary=f"{len(series)} rows of {series.symbol}",
        )


class MarketScreenTool(Tool):
    name = "MarketScreen"
    bulky = True
    description = f"""
Compute the indicator set across a list of symbols and return a table.

Use to compare a watchlist, or to find which names meet a condition. It
returns the numbers for every symbol; apply the condition yourself and say
which ones matched and why. Capped at {MARKET_SCREEN_LIMIT} symbols.
"""

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "symbols": {"type": "array", "items": {"type": "string"}},
                "bars": {"type": "integer", "description": "History depth, default 120"},
            },
            "required": ["symbols"],
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        symbols = [str(s).strip() for s in (args.get("symbols") or []) if str(s).strip()]
        if not symbols:
            return ToolResult.error("no symbols given")
        if len(symbols) > MARKET_SCREEN_LIMIT:
            return ToolResult.error(
                f"{len(symbols)} symbols exceeds the {MARKET_SCREEN_LIMIT} limit; "
                f"screen in batches"
            )

        try:
            router = _router(ctx)
        except MarketDataError as error:
            return ToolResult.error(str(error))

        depth = int(args.get("bars") or 120)
        header = (
            f"{'symbol':<12}{'close':>10}{'chg%':>8}{'MA5':>10}{'MA20':>10}"
            f"{'MA60':>10}{'RSI':>7}{'vol×':>7}"
        )
        rows: list[str] = []
        failures: list[str] = []

        for symbol in symbols:
            try:
                series = router.history(symbol, count=depth)
            except MarketDataError as error:
                failures.append(f"{symbol}: {error}")
                continue
            if series.empty:
                failures.append(f"{symbol}: no data")
                continue
            rows.append(_screen_row(series))

        if not rows:
            return ToolResult.error("nothing could be screened:\n" + "\n".join(failures))

        body = f"{header}\n" + "\n".join(rows)
        if failures:
            body += "\n\nSkipped:\n" + "\n".join(f"  {f}" for f in failures)
        return ToolResult(
            content=body + DATA_CAVEAT, summary=f"screened {len(rows)}/{len(symbols)} symbols"
        )


def _screen_row(series: BarSeries) -> str:
    """One row of the screen table."""
    indicators = indicator_module.compute(series)
    latest = indicators.latest()
    bar = series.latest

    def show(key: str, width: int, places: int = 2) -> str:
        value = latest.get(key)
        return f"{value:>{width}.{places}f}" if value is not None else " " * (width - 1) + "-"

    return (
        f"{series.symbol:<12}{bar.close:>10.2f}{bar.change * 100:>8.2f}"
        f"{show('MA5', 10)}{show('MA20', 10)}{show('MA60', 10)}"
        f"{show('RSI', 7, 1)}{show('volume_ratio', 7, 2)}"
    )


class PaperTradeTool(Tool):
    name = "PaperTrade"
    description = f"""
Buy or sell in the paper account. **No real money is involved and no order
reaches a broker.**

Give a real `reason`: the point of the paper account is to find out later
whether these calls were any good, and a trade with no stated rationale
cannot be reviewed. A-shares trade in lots of {LOT_SIZE_HINT}, and commission
and stamp duty are applied.

If the user asks you to place a real order, say plainly that this tool does
not do that and that they need to place it themselves.
"""

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["buy", "sell"]},
                "symbol": {"type": "string"},
                "quantity": {"type": "integer", "description": "Shares, a multiple of 100"},
                "price": {
                    "type": "number",
                    "description": "Fill price; the latest close is used when omitted",
                },
                "reason": {"type": "string", "description": "Why — recorded for later review"},
            },
            "required": ["action", "symbol", "quantity", "reason"],
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        book = getattr(ctx, "paper_book", None)
        if book is None:
            return ToolResult.error("the paper account is not configured")

        symbol = str(args.get("symbol", "")).strip().upper()
        action = str(args.get("action", "")).lower()
        if action not in ("buy", "sell"):
            return ToolResult.error("action must be 'buy' or 'sell'")

        price = args.get("price")
        if price is None:
            try:
                bar = _router(ctx).latest_bar(symbol)
            except MarketDataError as error:
                return ToolResult.error(str(error))
            if bar is None:
                return ToolResult.error(f"no price for {symbol}; pass one explicitly")
            price = bar.close

        portfolio = book.load()
        try:
            operation = portfolio.buy if action == "buy" else portfolio.sell
            fill = operation(
                symbol,
                int(args.get("quantity", 0)),
                float(price),
                reason=str(args.get("reason", "")),
            )
        except PaperTradeError as error:
            return ToolResult.error(str(error))
        book.save(portfolio)

        return ToolResult(
            content=(
                f"Paper trade recorded (no real order was placed):\n{fill.describe()}\n\n"
                f"{portfolio.describe()}"
            ),
            summary=f"paper {action} {fill.quantity} {symbol} @ {fill.price:.2f}",
            display={"kind": "paper_trade", "symbol": symbol, "side": action},
        )


class PaperAccountTool(Tool):
    name = "PaperAccount"
    description = """
Show the paper account: cash, positions, unrealised profit and the trade log.

Positions are marked against the latest available close.
"""

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "history": {"type": "boolean", "description": "Include recent trades"}
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        book = getattr(ctx, "paper_book", None)
        if book is None:
            return ToolResult.error("the paper account is not configured")

        portfolio = book.load()
        prices: dict[str, float] = {}
        router = getattr(ctx, "market", None)
        if router is not None:
            for symbol in portfolio.positions:
                bar = router.latest_bar(symbol)
                if bar is not None:
                    prices[symbol] = bar.close

        body = portfolio.describe(prices)
        if args.get("history") and portfolio.fills:
            recent = "\n".join(fill.describe() for fill in portfolio.fills[-20:])
            body += f"\n\nRecent trades:\n{recent}"
        return ToolResult(content=body, summary="paper account")


def market_tools() -> list[Tool]:
    """Every market tool. Registered only when enabled in config."""
    return [
        MarketChartTool(),
        MarketQuoteTool(),
        MarketHistoryTool(),
        MarketScreenTool(),
        PaperTradeTool(),
        PaperAccountTool(),
    ]
