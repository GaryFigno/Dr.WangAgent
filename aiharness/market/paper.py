"""A paper portfolio: positions, cash and a full trade log.

This is where the agent's trading ideas go. It is not a stepping stone to
live execution and there is no code path from here to a broker — see the
module note in :mod:`aiharness.tools.market` for why.

What it is good for: finding out whether the agent's calls are any good
before believing them. Every fill is recorded with the reasoning that
produced it, so a month later the question "was that a real edge or a lucky
run" has an answer sitting on disk.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

#: Chinese A-shares trade in lots of 100.
LOT_SIZE = 100
#: Default commission, as a fraction of notional.
DEFAULT_COMMISSION = 0.00025
#: Minimum commission per trade, in yuan.
MIN_COMMISSION = 5.0
#: Stamp duty on sells only, as a fraction of notional.
STAMP_DUTY = 0.0005
#: Trades retained in the log before the oldest are dropped.
TRADE_LOG_LIMIT = 2000


class PaperTradeError(Exception):
    """Raised for trades the portfolio cannot accept."""


@dataclass
class Fill:
    """One executed paper trade."""

    at: float
    day: str
    symbol: str
    side: str  # buy | sell
    quantity: int
    price: float
    fees: float
    #: Why this trade was made. The point of keeping it is later review.
    reason: str = ""
    actor: str = "agent"

    @property
    def notional(self) -> float:
        return self.quantity * self.price

    def describe(self) -> str:
        stamp = datetime.fromtimestamp(self.at).strftime("%Y-%m-%d %H:%M")
        return (
            f"{stamp}  {self.side.upper():4s} {self.symbol} "
            f"{self.quantity:>6d} @ {self.price:.2f} "
            f"= {self.notional:,.2f} (fees {self.fees:.2f})"
            + (f"\n    {self.reason}" if self.reason else "")
        )


@dataclass
class Position:
    """A holding, with the average cost it was accumulated at."""

    symbol: str
    quantity: int = 0
    average_cost: float = 0.0

    def market_value(self, price: float) -> float:
        return self.quantity * price

    def unrealised(self, price: float) -> float:
        return (price - self.average_cost) * self.quantity

    def unrealised_percent(self, price: float) -> float:
        if not self.average_cost:
            return 0.0
        return (price - self.average_cost) / self.average_cost


@dataclass
class Portfolio:
    """Cash, positions and realised profit."""

    cash: float
    initial_cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    realised: float = 0.0
    fills: list[Fill] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    # -- valuation --------------------------------------------------------

    def market_value(self, prices: dict[str, float]) -> float:
        return sum(
            position.market_value(prices.get(symbol, position.average_cost))
            for symbol, position in self.positions.items()
        )

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + self.market_value(prices)

    def total_return(self, prices: dict[str, float]) -> float:
        if not self.initial_cash:
            return 0.0
        return (self.equity(prices) - self.initial_cash) / self.initial_cash

    # -- trading ----------------------------------------------------------

    def buy(
        self,
        symbol: str,
        quantity: int,
        price: float,
        *,
        reason: str = "",
        day: date | None = None,
    ) -> Fill:
        """Buy, checking lot size and available cash.

        Raises:
          PaperTradeError: On a bad quantity, price, or insufficient cash.
        """
        self._validate(symbol, quantity, price)
        fees = commission(quantity * price, side="buy")
        cost = quantity * price + fees
        if cost > self.cash:
            raise PaperTradeError(
                f"need {cost:,.2f} (incl. {fees:.2f} fees) but only {self.cash:,.2f} is free"
            )

        position = self.positions.setdefault(symbol, Position(symbol))
        total_cost = position.average_cost * position.quantity + quantity * price
        position.quantity += quantity
        position.average_cost = total_cost / position.quantity
        self.cash -= cost
        return self._record(symbol, "buy", quantity, price, fees, reason, day)

    def sell(
        self,
        symbol: str,
        quantity: int,
        price: float,
        *,
        reason: str = "",
        day: date | None = None,
    ) -> Fill:
        """Sell, checking the position covers it.

        Raises:
          PaperTradeError: On a bad quantity or an oversell.
        """
        self._validate(symbol, quantity, price)
        position = self.positions.get(symbol)
        if position is None or position.quantity < quantity:
            held = position.quantity if position else 0
            raise PaperTradeError(f"holding {held} of {symbol}; cannot sell {quantity}")

        fees = commission(quantity * price, side="sell")
        self.realised += (price - position.average_cost) * quantity - fees
        self.cash += quantity * price - fees
        position.quantity -= quantity
        if position.quantity == 0:
            del self.positions[symbol]
        return self._record(symbol, "sell", quantity, price, fees, reason, day)

    def _validate(self, symbol: str, quantity: int, price: float) -> None:
        if not symbol.strip():
            raise PaperTradeError("no symbol given")
        if quantity <= 0:
            raise PaperTradeError("quantity must be positive")
        if quantity % LOT_SIZE:
            raise PaperTradeError(
                f"A-shares trade in lots of {LOT_SIZE}; {quantity} is not a whole lot"
            )
        if price <= 0:
            raise PaperTradeError("price must be positive")

    def _record(
        self,
        symbol: str,
        side: str,
        quantity: int,
        price: float,
        fees: float,
        reason: str,
        day: date | None,
    ) -> Fill:
        fill = Fill(
            at=time.time(),
            day=str(day or date.today()),
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            fees=fees,
            reason=reason.strip(),
        )
        self.fills.append(fill)
        del self.fills[:-TRADE_LOG_LIMIT]
        return fill

    # -- reporting --------------------------------------------------------

    def describe(self, prices: dict[str, float] | None = None) -> str:
        """A plain statement of the account. Numbers only, no judgement."""
        prices = prices or {}
        equity = self.equity(prices)
        lines = [
            f"Paper account — equity {equity:,.2f}, cash {self.cash:,.2f}, "
            f"realised {self.realised:+,.2f}, "
            f"total return {self.total_return(prices) * 100:+.2f}%",
            "",
        ]
        if not self.positions:
            lines.append("No open positions.")
        else:
            lines.append("Positions:")
            for symbol, position in sorted(self.positions.items()):
                price = prices.get(symbol, position.average_cost)
                marked = " (marked at cost — no live price)" if symbol not in prices else ""
                lines.append(
                    f"  {symbol}  {position.quantity:>6d} @ {position.average_cost:.2f}  "
                    f"now {price:.2f}  "
                    f"{position.unrealised(price):+,.2f} "
                    f"({position.unrealised_percent(price) * 100:+.2f}%){marked}"
                )
        lines += ["", f"{len(self.fills)} trade(s) recorded."]
        return "\n".join(lines)


def commission(notional: float, side: str) -> float:
    """Estimate A-share trading costs.

    Args:
      notional: Trade value before fees.
      side: ``buy`` or ``sell``; stamp duty applies to sells only.

    Returns:
      Total fees in yuan.
    """
    fee = max(notional * DEFAULT_COMMISSION, MIN_COMMISSION)
    if side == "sell":
        fee += notional * STAMP_DUTY
    return round(fee, 2)


class PaperBook:
    """Loads and saves a portfolio as JSON."""

    def __init__(self, path: Path, initial_cash: float = 500_000.0):
        self.path = Path(path)
        self.initial_cash = initial_cash

    def load(self) -> Portfolio:
        """Read the portfolio, creating a fresh one when absent or corrupt."""
        if not self.path.is_file():
            return Portfolio(cash=self.initial_cash, initial_cash=self.initial_cash)
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return Portfolio(cash=self.initial_cash, initial_cash=self.initial_cash)

        portfolio = Portfolio(
            cash=float(payload.get("cash", self.initial_cash)),
            initial_cash=float(payload.get("initial_cash", self.initial_cash)),
            realised=float(payload.get("realised", 0.0)),
            created_at=float(payload.get("created_at", time.time())),
        )
        for symbol, raw in (payload.get("positions") or {}).items():
            portfolio.positions[symbol] = Position(
                symbol=symbol,
                quantity=int(raw.get("quantity", 0)),
                average_cost=float(raw.get("average_cost", 0.0)),
            )
        for raw in payload.get("fills") or []:
            known = {k: v for k, v in raw.items() if k in Fill.__annotations__}
            try:
                portfolio.fills.append(Fill(**known))
            except TypeError:
                continue
        return portfolio

    def save(self, portfolio: Portfolio) -> None:
        """Persist the portfolio."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "cash": portfolio.cash,
            "initial_cash": portfolio.initial_cash,
            "realised": portfolio.realised,
            "created_at": portfolio.created_at,
            "positions": {s: asdict(p) for s, p in portfolio.positions.items()},
            "fills": [asdict(f) for f in portfolio.fills],
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
