"""Unit tests for :class:`FyersInstrumentResolver` — no network, fixture data only.

A fake symbol source returns a small slice of normalised option rows (as the
FYERS CSV master would yield). Covers nearest-expiry selection (works for weekly
*and* monthly cycles), CE/PE + strike/underlying filtering, the not-found case,
per-day caching, tolerant parsing (date / datetime / epoch expiry, malformed
rows), and the CSV parser's positional column mapping.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from teletrader.execution import (
    FyersCsvSymbolMaster,
    FyersInstrumentResolver,
    InstrumentNotFoundError,
    ResolvedInstrument,
)

_IST = ZoneInfo("Asia/Kolkata")


def _epoch(d: date) -> int:
    """Midnight-IST epoch seconds for ``d`` (how the master encodes expiry)."""
    return int(datetime(d.year, d.month, d.day, tzinfo=_IST).timestamp())


def _row(
    underlying: str,
    option_type: str,
    strike: Any,
    symbol: str,
    expiry: Any,
    *,
    lot_size: int = 75,
    exchange: str = "NSE",
) -> dict[str, Any]:
    return {
        "underlying": underlying,
        "option_type": option_type,
        "strike": strike,
        "symbol": symbol,
        "expiry": expiry,
        "lot_size": lot_size,
        "exchange": exchange,
    }


# A representative option master with decoys to exercise the filtering. Index
# options carry weekly expiries; a stock option carries a monthly expiry.
ROWS: list[dict[str, Any]] = [
    _row("NIFTY", "PE", 23900, "NSE:NIFTY2562623900PE", date(2026, 6, 26)),   # past
    _row("NIFTY", "PE", 23900.0, "NSE:NIFTY2570323900PE", _epoch(date(2026, 7, 3))),  # near (epoch + float strike)
    _row("NIFTY", "PE", 23900, "NSE:NIFTY2571023900PE", date(2026, 7, 10)),   # next
    _row("NIFTY", "CE", 23900, "NSE:NIFTY2570323900CE", date(2026, 7, 3)),    # CE decoy
    _row("NIFTY", "PE", 24000, "NSE:NIFTY2570324000PE", date(2026, 7, 3)),    # other strike
    _row("BANKNIFTY", "PE", 52000, "NSE:BANKNIFTY2573152000PE", datetime(2026, 7, 31)),  # datetime expiry
    _row("RELIANCE", "CE", 3000, "NSE:RELIANCE26JUL3000CE", date(2026, 7, 31), lot_size=250),  # monthly stock option
    {"underlying": "NIFTY", "option_type": "XX", "strike": 1, "symbol": "X", "expiry": date(2026, 7, 3)},  # not CE/PE
    {"underlying": "NIFTY", "option_type": "PE", "symbol": "BAD", "expiry": date(2026, 7, 3)},  # no strike -> skipped
]


class FakeSource:
    """Records how many times the (expensive) master is fetched."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.fetches = 0

    def option_rows(self) -> list[dict[str, Any]]:
        self.fetches += 1
        return self._rows


def test_resolves_nearest_expiry() -> None:
    resolver = FyersInstrumentResolver(FakeSource(ROWS))
    got = resolver.resolve("NIFTY", 23900, "PE", on_date=date(2026, 6, 29))
    assert got == ResolvedInstrument("NSE:NIFTY2570323900PE", "NSE", date(2026, 7, 3), 75)


def test_past_expiries_are_skipped() -> None:
    resolver = FyersInstrumentResolver(FakeSource(ROWS))
    got = resolver.resolve("NIFTY", 23900, "PE", on_date=date(2026, 7, 4))
    assert got.tradingsymbol == "NSE:NIFTY2571023900PE"


def test_expiry_day_itself_is_eligible() -> None:
    resolver = FyersInstrumentResolver(FakeSource(ROWS))
    got = resolver.resolve("NIFTY", 23900, "PE", on_date=date(2026, 7, 3))
    assert got.expiry == date(2026, 7, 3)


def test_call_and_put_are_distinguished() -> None:
    resolver = FyersInstrumentResolver(FakeSource(ROWS))
    ce = resolver.resolve("NIFTY", 23900, "CE", on_date=date(2026, 6, 29))
    assert ce.tradingsymbol == "NSE:NIFTY2570323900CE"


def test_underlying_is_isolated() -> None:
    resolver = FyersInstrumentResolver(FakeSource(ROWS))
    bn = resolver.resolve("BANKNIFTY", 52000, "PE", on_date=date(2026, 6, 29))
    assert bn.tradingsymbol == "NSE:BANKNIFTY2573152000PE"
    assert bn.expiry == date(2026, 7, 31)


def test_stock_option_monthly_resolves() -> None:
    # A stock option (monthly cycle) resolves through the same nearest-expiry path.
    resolver = FyersInstrumentResolver(FakeSource(ROWS))
    got = resolver.resolve("RELIANCE", 3000, "CE", on_date=date(2026, 7, 1))
    assert got.tradingsymbol == "NSE:RELIANCE26JUL3000CE"
    assert got.lot_size == 250


def test_unknown_strike_raises_not_found() -> None:
    resolver = FyersInstrumentResolver(FakeSource(ROWS))
    with pytest.raises(InstrumentNotFoundError):
        resolver.resolve("NIFTY", 99999, "PE", on_date=date(2026, 6, 29))


def test_all_expiries_in_the_past_raises_not_found() -> None:
    resolver = FyersInstrumentResolver(FakeSource(ROWS))
    with pytest.raises(InstrumentNotFoundError):
        resolver.resolve("NIFTY", 23900, "PE", on_date=date(2027, 1, 1))


def test_master_is_cached_per_day() -> None:
    source = FakeSource(ROWS)
    resolver = FyersInstrumentResolver(source)
    resolver.resolve("NIFTY", 23900, "PE", on_date=date(2026, 6, 29))
    resolver.resolve("NIFTY", 24000, "PE", on_date=date(2026, 6, 29))
    assert source.fetches == 1  # same day -> fetched once

    resolver.resolve("NIFTY", 23900, "PE", on_date=date(2026, 6, 30))
    assert source.fetches == 2  # new trading day -> refetched


def test_malformed_rows_do_not_break_the_load() -> None:
    resolver = FyersInstrumentResolver(FakeSource(ROWS))
    got = resolver.resolve("NIFTY", 23900, "PE", on_date=date(2026, 6, 29))
    assert got.tradingsymbol == "NSE:NIFTY2570323900PE"


# --- CSV symbol master (positional column mapping) ----------------------------


def _csv_line(
    *, underlying: str, option_type: str, strike: str, symbol: str, expiry_epoch: int
) -> str:
    """Build one FYERS-master CSV row placing values at the documented indices."""
    cols = [""] * 17
    cols[3] = "75"            # lot size
    cols[8] = str(expiry_epoch)
    cols[9] = symbol
    cols[10] = "NSE"
    cols[13] = underlying
    cols[15] = strike
    cols[16] = option_type
    return ",".join(cols)


def test_csv_master_parses_and_resolves() -> None:
    csv_text = "\n".join(
        [
            "some,equity,row,with,too,few,columns",  # short row -> skipped
            _csv_line(
                underlying="NIFTY", option_type="PE", strike="23900",
                symbol="NSE:NIFTY2570323900PE", expiry_epoch=_epoch(date(2026, 7, 3)),
            ),
            _csv_line(
                underlying="NIFTY", option_type="CE", strike="23900",
                symbol="NSE:NIFTY2570323900CE", expiry_epoch=_epoch(date(2026, 7, 3)),
            ),
        ]
    )
    master = FyersCsvSymbolMaster(urls=("http://x/NSE_FO.csv",), downloader=lambda _url: csv_text)
    resolver = FyersInstrumentResolver(master)
    got = resolver.resolve("NIFTY", 23900, "PE", on_date=date(2026, 7, 1))
    assert got.tradingsymbol == "NSE:NIFTY2570323900PE"
    assert got.lot_size == 75
    assert got.exchange == "NSE"
