"""Market data, indicators, charting and the paper account."""

from __future__ import annotations

import array
import math
from datetime import date, timedelta

import pytest

from aiharness.market import chart, indicators
from aiharness.market.bars import Bar, BarSeries, build_series, parse_day
from aiharness.market.paper import (
    LOT_SIZE,
    PaperBook,
    PaperTradeError,
    Portfolio,
    commission,
)
from aiharness.market.qlib_store import QlibStore, QlibStoreError
from aiharness.market.router import MarketDataError, MarketRouter
from aiharness.permissions import PermissionEngine
from aiharness.tools.base import ToolContext
from aiharness.tools.market import (
    MarketChartTool,
    PaperAccountTool,
    PaperTradeTool,
    market_tools,
)
from aiharness.toolset import build_registry

# -- fixtures --------------------------------------------------------------


def make_series(closes: list[float], symbol: str = "SH000001") -> BarSeries:
    """Build a series with plausible OHLC around the given closes."""
    start = date(2026, 1, 5)
    bars = []
    for index, close in enumerate(closes):
        previous = closes[index - 1] if index else close
        bars.append(
            Bar(
                day=start + timedelta(days=index),
                open=previous,
                high=max(previous, close) * 1.01,
                low=min(previous, close) * 0.99,
                close=close,
                volume=1000.0 + index * 10,
            )
        )
    return BarSeries(symbol=symbol, bars=bars, source="test")


@pytest.fixture
def fake_store(tmp_path):
    """A minimal but real qlib store, written in the true binary layout."""
    root = tmp_path / "cn_data"
    (root / "calendars").mkdir(parents=True)
    (root / "instruments").mkdir()
    features = root / "features" / "sh600000"
    features.mkdir(parents=True)

    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(10)]
    (root / "calendars" / "day.txt").write_text(
        "\n".join(str(d) for d in days), encoding="utf-8"
    )
    (root / "instruments" / "all.txt").write_text(
        f"SH600000\t{days[0]}\t{days[-1]}\n", encoding="utf-8"
    )

    closes = [10.0 + i for i in range(10)]
    columns = {
        "open": [c - 0.5 for c in closes],
        "high": [c + 0.5 for c in closes],
        "low": [c - 1.0 for c in closes],
        "close": closes,
        "volume": [1000.0 * (i + 1) for i in range(10)],
    }
    for name, values in columns.items():
        # Element 0 is the calendar start index, not a price.
        payload = array.array("f", [0.0, *values])
        (features / f"{name}.day.bin").write_bytes(payload.tobytes())
    return root


# -- bars ------------------------------------------------------------------


def test_bar_direction_and_change():
    up = Bar(date(2026, 1, 5), open=10.0, high=11.0, low=9.9, close=10.5, volume=1.0)
    down = Bar(date(2026, 1, 6), open=10.5, high=10.6, low=9.0, close=9.5, volume=1.0)
    assert up.rising and not down.rising
    assert up.change == pytest.approx(0.05)
    assert down.change < 0


def test_series_summary_states_the_adjustment():
    series = make_series([10.0, 11.0, 12.0])
    assert "backward-adjusted" in series.describe()
    assert series.period_return() == pytest.approx(0.2)


def test_max_drawdown_measures_peak_to_trough():
    series = make_series([100.0, 120.0, 60.0, 80.0])
    assert series.max_drawdown() == pytest.approx(-0.5)


def test_build_series_drops_rows_with_bad_prices():
    days = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]
    series = build_series(
        "X", days,
        opens=[10.0, float("nan"), 12.0],
        highs=[11.0, 11.0, 13.0],
        lows=[9.0, 9.0, 11.0],
        closes=[10.5, 11.0, 12.5],
        volumes=[1.0, 1.0, 1.0],
    )
    assert len(series) == 2  # the NaN row is dropped, not filled


def test_build_series_drops_non_positive_prices():
    days = [date(2026, 1, 5), date(2026, 1, 6)]
    series = build_series(
        "X", days, [10.0, 0.0], [11.0, 0.0], [9.0, 0.0], [10.5, 0.0], [1.0, 1.0]
    )
    assert len(series) == 1


def test_parse_day_accepts_a_timestamp_prefix():
    assert parse_day("2026-07-17 15:00:00") == date(2026, 7, 17)


# -- qlib store ------------------------------------------------------------


def test_store_reads_the_binary_layout(fake_store):
    store = QlibStore(fake_store)
    assert store.available
    series = store.bars("SH600000")
    assert len(series) == 10
    assert series.bars[0].close == pytest.approx(10.0)
    assert series.bars[-1].close == pytest.approx(19.0)
    assert series.bars[0].day == date(2026, 1, 5)


def test_store_accepts_the_symbol_shapes_people_type(fake_store):
    store = QlibStore(fake_store)
    for spelling in ("SH600000", "sh600000", "600000", "600000.SH"):
        resolved = store.resolve(spelling)
        assert resolved is not None, spelling
        assert resolved.symbol == "SH600000"


def test_store_points_at_cn_data_when_given_its_parent(fake_store):
    store = QlibStore(fake_store.parent)
    assert store.available
    assert store.root.name == "cn_data"


def test_store_reports_a_missing_symbol_clearly(fake_store):
    with pytest.raises(QlibStoreError) as error:
        QlibStore(fake_store).bars("SH999999")
    assert "not in the all universe" in str(error.value)


def test_store_reports_a_missing_store(tmp_path):
    with pytest.raises(QlibStoreError):
        QlibStore(tmp_path / "nothing").bars("SH600000")


def test_misaligned_fields_are_aligned_not_mixed(fake_store):
    """Fields can start on different calendar days; rows must stay honest."""
    features = fake_store / "features" / "sh600000"
    # Rewrite close to start two days later than the other fields.
    shifted = array.array("f", [2.0, *[100.0 + i for i in range(8)]])
    (features / "close.day.bin").write_bytes(shifted.tobytes())

    from aiharness.market.qlib_store import clear_caches

    clear_caches()
    series = QlibStore(fake_store).bars("SH600000")
    assert series.bars[0].day == date(2026, 1, 7)  # the later start wins
    assert series.bars[0].close == pytest.approx(100.0)


# -- indicators ------------------------------------------------------------


def test_moving_averages_pad_the_head_with_none():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    result = indicators.sma(values, 3)
    assert result[:2] == [None, None]
    assert result[2] == pytest.approx(2.0)
    assert result[-1] == pytest.approx(4.0)
    assert len(result) == len(values)


def test_ema_is_seeded_from_the_first_sma():
    values = [float(v) for v in range(1, 11)]
    result = indicators.ema(values, 3)
    assert result[1] is None
    assert result[2] == pytest.approx(2.0)
    assert result[-1] > result[2]


def test_rsi_is_bounded_and_reads_a_pure_uptrend_as_overbought():
    rising = [float(v) for v in range(1, 40)]
    result = indicators.rsi(rising)
    latest = result[-1]
    assert latest is not None
    assert 0.0 <= latest <= 100.0
    assert latest > 90


def test_rsi_reads_a_pure_downtrend_as_oversold():
    falling = [float(v) for v in range(40, 1, -1)]
    latest = indicators.rsi(falling)[-1]
    assert latest is not None
    assert latest < 10


def test_macd_components_stay_aligned():
    values = [10.0 + math.sin(i / 3) for i in range(80)]
    result = indicators.macd(values)
    assert len(result.macd) == len(result.signal) == len(result.histogram) == len(values)
    for index in range(len(values)):
        if result.macd[index] is not None and result.signal[index] is not None:
            assert result.histogram[index] == pytest.approx(
                result.macd[index] - result.signal[index]
            )


def test_bollinger_bands_bracket_the_middle():
    values = [10.0 + math.sin(i / 4) for i in range(60)]
    bands = indicators.bollinger(values)
    for upper, middle, lower in zip(
        bands.upper, bands.middle, bands.lower, strict=True
    ):
        if middle is None:
            continue
        assert lower <= middle <= upper


def test_true_range_accounts_for_gaps():
    highs, lows, closes = [10.0, 20.0], [9.0, 19.0], [9.5, 19.5]
    ranges = indicators.true_range(highs, lows, closes)
    # The second bar gapped up from 9.5 to a 19-20 range.
    assert ranges[1] == pytest.approx(20.0 - 9.5)


def test_indicator_set_summarises_the_latest_values():
    series = make_series([10.0 + math.sin(i / 5) for i in range(80)])
    latest = indicators.compute(series).latest()
    for key in ("MA5", "MA20", "RSI", "MACD", "BOLL_upper", "ATR"):
        assert latest[key] is not None, key


def test_indicators_on_a_short_series_do_not_crash():
    series = make_series([10.0, 10.5])
    latest = indicators.compute(series).latest()
    assert latest["MA60"] is None  # not enough history, reported honestly


# -- charting --------------------------------------------------------------


def test_chart_renders_candles_and_axes():
    # Short enough to fit the plot area, so the whole span is on the axis.
    series = make_series([10.0 + math.sin(i / 4) for i in range(30)])
    markup = chart.render(series, width=60, height=10, volume_height=3)
    assert "█" in markup or "▄" in markup or "▀" in markup
    assert str(series.first.day) in markup
    assert str(series.latest.day) in markup


def test_chart_shows_only_the_window_that_fits():
    """A long series is trimmed to the plot width, and the axis says so."""
    series = make_series([10.0 + math.sin(i / 4) for i in range(400)])
    markup = chart.render(series, width=60, height=10, volume_height=3)
    assert str(series.latest.day) in markup  # the recent end is always shown
    assert str(series.first.day) not in markup  # the distant past is not


def test_chart_uses_red_for_up_and_green_for_down():
    """Chinese convention. Inverting this would flip every chart's meaning."""
    rising = make_series([10.0, 11.0, 12.0, 13.0])
    falling = make_series([13.0, 12.0, 11.0, 10.0])
    assert chart.COLOUR_UP in chart.render(rising, width=40, height=6)
    assert chart.COLOUR_DOWN in chart.render(falling, width=40, height=6)


def test_chart_handles_an_empty_series():
    assert "no data" in chart.render(BarSeries(symbol="X"))


def test_chart_handles_a_flat_series():
    flat = make_series([10.0] * 20)
    markup = chart.render(flat, width=50, height=8)
    assert markup  # a zero-range series must not divide by zero


def test_summary_reports_facts_without_advice():
    series = make_series([10.0 + i * 0.1 for i in range(40)])
    text = chart.summarise(series, indicators.compute(series))
    assert "period return" in text
    for word in ("buy", "sell", "recommend", "should"):
        assert word not in text.lower()


# -- paper account ---------------------------------------------------------


def test_buy_then_sell_tracks_cost_and_profit():
    portfolio = Portfolio(cash=100_000.0, initial_cash=100_000.0)
    portfolio.buy("SH600000", 1000, 10.0, reason="test")
    assert portfolio.positions["SH600000"].quantity == 1000
    assert portfolio.cash < 90_000.0  # cash minus fees

    portfolio.sell("SH600000", 1000, 12.0, reason="test")
    assert "SH600000" not in portfolio.positions
    assert portfolio.realised > 1_900  # 2000 profit less fees


def test_average_cost_blends_across_buys():
    portfolio = Portfolio(cash=100_000.0, initial_cash=100_000.0)
    portfolio.buy("X", 1000, 10.0, reason="a")
    portfolio.buy("X", 1000, 20.0, reason="b")
    assert portfolio.positions["X"].average_cost == pytest.approx(15.0)


def test_odd_lots_are_refused():
    portfolio = Portfolio(cash=100_000.0, initial_cash=100_000.0)
    with pytest.raises(PaperTradeError) as error:
        portfolio.buy("X", 150, 10.0, reason="odd")
    assert str(LOT_SIZE) in str(error.value)


def test_buying_beyond_cash_is_refused():
    portfolio = Portfolio(cash=1_000.0, initial_cash=1_000.0)
    with pytest.raises(PaperTradeError):
        portfolio.buy("X", 1000, 10.0, reason="too big")


def test_overselling_is_refused():
    portfolio = Portfolio(cash=100_000.0, initial_cash=100_000.0)
    portfolio.buy("X", 100, 10.0, reason="a")
    with pytest.raises(PaperTradeError) as error:
        portfolio.sell("X", 200, 11.0, reason="b")
    assert "cannot sell" in str(error.value)


def test_stamp_duty_applies_to_sells_only():
    assert commission(100_000.0, "sell") > commission(100_000.0, "buy")


def test_commission_has_a_floor():
    assert commission(100.0, "buy") == pytest.approx(5.0)


def test_portfolio_round_trips_through_disk(tmp_path):
    book = PaperBook(tmp_path / "paper.json", initial_cash=50_000.0)
    portfolio = book.load()
    portfolio.buy("SH600000", 1000, 10.0, reason="because")
    book.save(portfolio)

    reloaded = PaperBook(tmp_path / "paper.json").load()
    assert reloaded.positions["SH600000"].quantity == 1000
    assert reloaded.fills[0].reason == "because"


def test_a_corrupt_paper_file_starts_fresh(tmp_path):
    path = tmp_path / "paper.json"
    path.write_text("{not json", encoding="utf-8")
    portfolio = PaperBook(path, initial_cash=1234.0).load()
    assert portfolio.cash == pytest.approx(1234.0)


# -- the router ------------------------------------------------------------


def test_router_prefers_the_local_store(fake_store):
    router = MarketRouter(fake_store, allow_live=False)
    assert router.has_store
    series = router.history("SH600000", count=5)
    assert len(series) == 5
    assert series.source.startswith("qlib")


def test_router_without_any_source_says_so(tmp_path):
    router = MarketRouter(tmp_path / "nothing", allow_live=False)
    with pytest.raises(MarketDataError) as error:
        router.history("SH600000")
    assert "no source" in str(error.value)


def test_router_refuses_quotes_when_live_is_disabled(fake_store):
    router = MarketRouter(fake_store, allow_live=False)
    with pytest.raises(MarketDataError) as error:
        router.quote("SH600000")
    assert "disabled" in str(error.value)


def test_router_describes_its_sources(fake_store):
    description = MarketRouter(fake_store, allow_live=True).describe_sources()
    assert "qlib store" in description
    assert "ready" in description


# -- tools -----------------------------------------------------------------


def ctx_for(config, workspace, router_obj, **kwargs) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        config=config,
        permissions=PermissionEngine(config.permissions, workspace),
        router=router_obj,
        **kwargs,
    )


async def test_chart_tool_returns_chart_and_numbers(config, workspace, router, fake_store):
    ctx = ctx_for(
        config, workspace, router, market=MarketRouter(fake_store, allow_live=False)
    )
    result = await MarketChartTool().run({"symbol": "SH600000"}, ctx)
    assert not result.is_error
    assert "SH600000" in result.content
    assert "backward-adjusted" in result.content
    await router.aclose()


async def test_chart_tool_without_market_configured(config, workspace, router):
    ctx = ctx_for(config, workspace, router)
    result = await MarketChartTool().run({"symbol": "SH600000"}, ctx)
    assert result.is_error
    assert "not configured" in result.content
    await router.aclose()


async def test_paper_trade_tool_records_and_reports(config, workspace, router, fake_store, tmp_path):
    ctx = ctx_for(
        config, workspace, router,
        market=MarketRouter(fake_store, allow_live=False),
        paper_book=PaperBook(tmp_path / "paper.json", 100_000.0),
    )
    result = await PaperTradeTool().run(
        {"action": "buy", "symbol": "SH600000", "quantity": 1000,
         "reason": "testing the tool"},
        ctx,
    )
    assert not result.is_error
    assert "no real order was placed" in result.content

    account = await PaperAccountTool().run({"history": True}, ctx)
    assert "SH600000" in account.content
    assert "testing the tool" in account.content
    await router.aclose()


async def test_paper_trade_requires_a_reason(config, workspace, router, tmp_path):
    ctx = ctx_for(
        config, workspace, router,
        paper_book=PaperBook(tmp_path / "paper.json", 100_000.0),
    )
    await PaperTradeTool().run(
        {"action": "buy", "symbol": "X", "quantity": 100, "price": 10.0, "reason": ""}, ctx
    )
    # A reason is schema-required; an empty one still records but the schema
    # marks it required, so the model is told to supply one.
    assert "reason" in str(PaperTradeTool().schema()["required"])
    await router.aclose()


def test_there_is_no_live_order_tool():
    """The absence of a real-trading tool is a deliberate design property."""
    names = {tool.name for tool in market_tools()}
    for forbidden in ("PlaceOrder", "Trade", "Buy", "Sell", "SubmitOrder", "Broker"):
        assert forbidden not in names
    assert "PaperTrade" in names


def test_market_tools_appear_only_when_enabled():
    plain = set(build_registry().names())
    enabled = set(build_registry(include_market=True).names())
    assert "MarketChart" in enabled - plain
    assert not any(n.startswith("Market") or n.startswith("Paper") for n in plain)
