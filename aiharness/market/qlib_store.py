"""Reader for a qlib binary data store.

This is the format AIQuant's ``quantloop`` already maintains, so pointing at
it gives the agent the *same* history the backtests run on. Reading it
directly rather than importing qlib matters: qlib pulls in pandas, numpy and
a large dependency tree, and all that is needed here is to seek into a flat
array of float32.

Layout, verified against the live store:

    calendars/day.txt              one YYYY-MM-DD per line, ascending
    instruments/all.txt            SYMBOL<TAB>start<TAB>end
    features/<lower>/<field>.day.bin

Each ``.day.bin`` is little-endian float32. Element 0 is not a price: it is
the index into the calendar at which the series starts. Everything after it
is one value per trading day, contiguous.

Stored prices are backward-adjusted (``stored = raw * factor``), which is
what makes them comparable across splits but not equal to the printed price.
"""

from __future__ import annotations

import array
import sys
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

from ..constants import INSTRUMENT_LINE_FIELDS
from .bars import BarSeries, build_series, parse_day

#: Fields read for a full OHLCV bar, in the order build_series wants them.
PRICE_FIELDS = ("open", "high", "low", "close", "volume", "amount")
#: Directories a qlib store is expected to contain.
REQUIRED_DIRS = ("calendars", "instruments", "features")
#: Cached calendars, keyed by store path.
CALENDAR_CACHE_SIZE = 8


class QlibStoreError(Exception):
    """Raised when a store is missing, malformed, or lacks an instrument."""


def _read_floats(path: Path) -> array.array:
    """Read a little-endian float32 file into an array."""
    values = array.array("f")
    with path.open("rb") as handle:
        values.frombytes(handle.read())
    if sys.byteorder != "little":  # pragma: no cover - depends on hardware
        values.byteswap()
    return values


@dataclass
class Instrument:
    """One tradable symbol and the span the store covers for it."""

    symbol: str
    start: date
    end: date

    @property
    def folder(self) -> str:
        """Feature directory name: qlib lowercases these."""
        return self.symbol.lower()


class QlibStore:
    """Read-only access to one qlib binary store."""

    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser()
        # Point at either the store root or its parent; both are common.
        if not (self.root / "calendars").is_dir() and (self.root / "cn_data").is_dir():
            self.root = self.root / "cn_data"

    # -- validation -------------------------------------------------------

    @property
    def available(self) -> bool:
        return all((self.root / name).is_dir() for name in REQUIRED_DIRS)

    def require(self) -> None:
        """Raise if this is not a usable store.

        Raises:
          QlibStoreError: With a message naming what is missing.
        """
        if not self.root.exists():
            raise QlibStoreError(f"no qlib store at {self.root}")
        missing = [name for name in REQUIRED_DIRS if not (self.root / name).is_dir()]
        if missing:
            raise QlibStoreError(
                f"{self.root} is not a qlib store; missing {', '.join(missing)}/"
            )

    # -- calendar ---------------------------------------------------------

    def calendar(self) -> list[date]:
        """Every trading day in the store, ascending."""
        return _load_calendar(str(self.root))

    # -- instruments ------------------------------------------------------

    def instruments(self, universe: str = "all") -> dict[str, Instrument]:
        """Load an instrument list such as ``all`` or ``csi300``.

        Raises:
          QlibStoreError: If the universe file does not exist.
        """
        return _load_instruments(str(self.root), universe)

    def resolve(self, symbol: str, universe: str = "all") -> Instrument | None:
        """Find an instrument by symbol, tolerating common input shapes.

        Accepts ``SH600000``, ``sh600000``, ``600000.SH`` and bare ``600000``,
        because users type all four and none of them is wrong.
        """
        table = self.instruments(universe)
        cleaned = symbol.strip().upper().replace(" ", "")
        if cleaned in table:
            return table[cleaned]

        if "." in cleaned:  # 600000.SH -> SH600000
            code, _, market = cleaned.partition(".")
            candidate = f"{market}{code}"
            if candidate in table:
                return table[candidate]

        if cleaned.isdigit():  # bare code: try each market prefix
            for prefix in ("SH", "SZ", "BJ"):
                if f"{prefix}{cleaned}" in table:
                    return table[f"{prefix}{cleaned}"]
        return None

    def search(self, query: str, limit: int = 20, universe: str = "all") -> list[str]:
        """Symbols containing ``query``."""
        needle = query.strip().upper()
        return sorted(s for s in self.instruments(universe) if needle in s)[:limit]

    # -- bars -------------------------------------------------------------

    def field(self, instrument: Instrument, name: str) -> tuple[int, array.array]:
        """Read one feature file.

        Returns:
          ``(start_index, values)`` where ``start_index`` indexes the calendar.

        Raises:
          QlibStoreError: If the field file is absent.
        """
        path = self.root / "features" / instrument.folder / f"{name}.day.bin"
        if not path.is_file():
            raise QlibStoreError(f"{instrument.symbol} has no '{name}' field at {path}")
        raw = _read_floats(path)
        if not raw:
            return 0, array.array("f")
        return int(raw[0]), raw[1:]

    def bars(self, symbol: str, *, universe: str = "all") -> BarSeries:
        """Load the full history for one symbol.

        Args:
          symbol: Any of the accepted symbol shapes.
          universe: Instrument list to resolve against.

        Returns:
          A :class:`~aiharness.market.bars.BarSeries`, backward-adjusted.

        Raises:
          QlibStoreError: If the store or the symbol is unusable.
        """
        self.require()
        instrument = self.resolve(symbol, universe)
        if instrument is None:
            raise QlibStoreError(
                f"'{symbol}' is not in the {universe} universe of {self.root}"
            )

        calendar = self.calendar()
        # Keep each field's own start index: they are not guaranteed equal,
        # and mixing them up silently shifts prices onto the wrong dates.
        starts: dict[str, int] = {}
        columns: dict[str, list[float]] = {}
        for name in PRICE_FIELDS:
            try:
                start, values = self.field(instrument, name)
            except QlibStoreError:
                if name == "amount":  # optional field, absent in some dumps
                    continue
                raise
            starts[name] = start
            columns[name] = list(values)

        required = [name for name in PRICE_FIELDS if name != "amount"]
        if not all(columns.get(name) for name in required):
            raise QlibStoreError(f"{instrument.symbol} has no usable price data")

        # Align every column on the latest start, so each row is complete.
        aligned_start = max(starts[name] for name in columns)
        for name, values in list(columns.items()):
            offset = aligned_start - starts[name]
            columns[name] = values[offset:] if offset > 0 else values

        length = min(len(columns[name]) for name in required)
        if length <= 0:
            raise QlibStoreError(f"{instrument.symbol} has no overlapping price data")
        days = calendar[aligned_start : aligned_start + length]

        return build_series(
            instrument.symbol,
            days,
            columns["open"][:length],
            columns["high"][:length],
            columns["low"][:length],
            columns["close"][:length],
            columns["volume"][:length],
            columns.get("amount") or None,
            source=f"qlib:{self.root.name}",
            adjusted=True,
        )


# --------------------------------------------------------------------------
# caching
# --------------------------------------------------------------------------


@lru_cache(maxsize=CALENDAR_CACHE_SIZE)
def _load_calendar(root: str) -> list[date]:
    path = Path(root) / "calendars" / "day.txt"
    if not path.is_file():
        raise QlibStoreError(f"no calendar at {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise QlibStoreError(f"cannot read {path}: {error}") from error
    return [parse_day(line) for line in text.split() if line.strip()]


@lru_cache(maxsize=CALENDAR_CACHE_SIZE)
def _load_instruments(root: str, universe: str) -> dict[str, Instrument]:
    path = Path(root) / "instruments" / f"{universe}.txt"
    if not path.is_file():
        available = sorted(p.stem for p in (Path(root) / "instruments").glob("*.txt"))
        raise QlibStoreError(
            f"no universe '{universe}'. Available: {', '.join(available) or '(none)'}"
        )
    table: dict[str, Instrument] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) < INSTRUMENT_LINE_FIELDS:
            continue
        try:
            table[parts[0].upper()] = Instrument(
                symbol=parts[0].upper(), start=parse_day(parts[1]), end=parse_day(parts[2])
            )
        except ValueError:
            continue
    return table


def clear_caches() -> None:
    """Drop cached calendars and instrument lists, e.g. after a data refresh."""
    _load_calendar.cache_clear()
    _load_instruments.cache_clear()
