"""Unit tests for :class:`KiteInstrumentResolver` — no network, fixture data only.

A fake instrument source returns a small, hand-built slice of what
``kite.instruments("NFO")`` yields. Covers nearest-weekly expiry selection, CE/PE
and strike/underlying filtering, the not-found case, per-day caching, and tolerant
parsing (datetime expiry, float strike, malformed rows).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pytest

from teletrader.execution import (
    InstrumentNotFoundError,
    KiteInstrumentResolver,
    ResolvedInstrument,
)


def _row(
    name: str,
    itype: str,
    strike: Any,
    tradingsymbol: str,
    expiry: Any,
    *,
    lot_size: int = 65,
    exchange: str = "NFO",
) -> dict[str, Any]:
    return {
        "name": name,
        "instrument_type": itype,
        "strike": strike,
        "tradingsymbol": tradingsymbol,
        "expiry": expiry,
        "lot_size": lot_size,
        "exchange": exchange,
        "segment": "NFO-OPT",
    }


# A representative NFO option master with decoys to exercise the filtering.
ROWS: list[dict[str, Any]] = [
    _row("NIFTY", "PE", 23900, "NIFTY2562623900PE", date(2026, 6, 26)),   # past
    _row("NIFTY", "PE", 23900.0, "NIFTY2570323900PE", date(2026, 7, 3)),  # near (float strike)
    _row("NIFTY", "PE", 23900, "NIFTY2571023900PE", date(2026, 7, 10)),   # next
    _row("NIFTY", "CE", 23900, "NIFTY2570323900CE", date(2026, 7, 3)),    # CE decoy
    _row("NIFTY", "PE", 24000, "NIFTY2570324000PE", date(2026, 7, 3)),    # other strike
    _row("BANKNIFTY", "PE", 23900, "BANKNIFTY26JUL23900PE", datetime(2026, 7, 31, 0, 0)),  # datetime expiry
    _row("FINNIFTY", "PE", 23900, "FINNIFTY2570323900PE", date(2026, 7, 3)),  # other underlying
    _row("NIFTY", "FUT", 0, "NIFTYFUT", date(2026, 7, 3)),                # future, skipped
    {"name": "NIFTY", "instrument_type": "PE", "tradingsymbol": "BAD", "expiry": date(2026, 7, 3)},  # no strike -> skipped
]


class FakeSource:
    """Records how many times the (expensive) instrument dump is fetched."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.fetches = 0

    def instruments(self, exchange: str) -> list[dict[str, Any]]:
        assert exchange == "NFO"
        self.fetches += 1
        return self._rows


def test_resolves_nearest_weekly_expiry() -> None:
    resolver = KiteInstrumentResolver(FakeSource(ROWS))
    got = resolver.resolve("NIFTY", 23900, "PE", on_date=date(2026, 6, 29))
    assert got == ResolvedInstrument("NIFTY2570323900PE", "NFO", date(2026, 7, 3), 65)


def test_past_expiries_are_skipped() -> None:
    resolver = KiteInstrumentResolver(FakeSource(ROWS))
    # After the 3 Jul expiry, the nearest upcoming is 10 Jul.
    got = resolver.resolve("NIFTY", 23900, "PE", on_date=date(2026, 7, 4))
    assert got.tradingsymbol == "NIFTY2571023900PE"


def test_expiry_day_itself_is_eligible() -> None:
    resolver = KiteInstrumentResolver(FakeSource(ROWS))
    got = resolver.resolve("NIFTY", 23900, "PE", on_date=date(2026, 7, 3))
    assert got.expiry == date(2026, 7, 3)


def test_call_and_put_are_distinguished() -> None:
    resolver = KiteInstrumentResolver(FakeSource(ROWS))
    ce = resolver.resolve("NIFTY", 23900, "CE", on_date=date(2026, 6, 29))
    assert ce.tradingsymbol == "NIFTY2570323900CE"


def test_underlying_is_isolated() -> None:
    resolver = KiteInstrumentResolver(FakeSource(ROWS))
    bn = resolver.resolve("BANKNIFTY", 23900, "PE", on_date=date(2026, 6, 29))
    assert bn.tradingsymbol == "BANKNIFTY26JUL23900PE"  # datetime expiry normalised
    assert bn.expiry == date(2026, 7, 31)


def test_unknown_strike_raises_not_found() -> None:
    resolver = KiteInstrumentResolver(FakeSource(ROWS))
    with pytest.raises(InstrumentNotFoundError):
        resolver.resolve("NIFTY", 99999, "PE", on_date=date(2026, 6, 29))


def test_all_expiries_in_the_past_raises_not_found() -> None:
    resolver = KiteInstrumentResolver(FakeSource(ROWS))
    with pytest.raises(InstrumentNotFoundError):
        resolver.resolve("NIFTY", 23900, "PE", on_date=date(2027, 1, 1))


def test_instrument_dump_is_cached_per_day() -> None:
    source = FakeSource(ROWS)
    resolver = KiteInstrumentResolver(source)
    resolver.resolve("NIFTY", 23900, "PE", on_date=date(2026, 6, 29))
    resolver.resolve("NIFTY", 24000, "PE", on_date=date(2026, 6, 29))
    assert source.fetches == 1  # same day -> fetched once

    resolver.resolve("NIFTY", 23900, "PE", on_date=date(2026, 6, 30))
    assert source.fetches == 2  # new trading day -> refetched


def test_malformed_rows_do_not_break_the_load() -> None:
    # The "BAD" row (no strike) and the FUT row must be skipped silently.
    resolver = KiteInstrumentResolver(FakeSource(ROWS))
    got = resolver.resolve("NIFTY", 23900, "PE", on_date=date(2026, 6, 29))
    assert got.tradingsymbol == "NIFTY2570323900PE"
