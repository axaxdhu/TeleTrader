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


# --- Underlying matching + lot sizes ------------------------------------------
#
# Signals name instruments the way people speak ("Apollo hospital"); the master
# uses exchange tickers ("APOLLOHOSP"). Bridging that gap is what lets a stock
# channel resolve at all — but a wrong match would buy the wrong company, so the
# rule is deliberately narrow and refuses when it is unsure.

ON = date(2026, 7, 1)

NAME_ROWS: list[dict[str, Any]] = [
    _row("APOLLOHOSP", "CE", 7500, "NSE:APOLLOHOSP26JUL7500CE", date(2026, 7, 31), lot_size=125),
    _row("APOLLOTYRE", "CE", 600, "NSE:APOLLOTYRE26JUL600CE", date(2026, 7, 31), lot_size=1700),
    _row("COFORGE", "CE", 1500, "NSE:COFORGE26JUL1500CE", date(2026, 7, 31), lot_size=150),
]


def _name_resolver() -> FyersInstrumentResolver:
    return FyersInstrumentResolver(FakeSource(NAME_ROWS))


def test_spoken_name_resolves_to_the_exchange_ticker() -> None:
    # "Apollo hospital" is how the signal writes it; APOLLOHOSP is how the
    # exchange does.
    resolved = _name_resolver().resolve("Apollo hospital", 7500, "CE", on_date=ON)
    assert resolved.tradingsymbol == "NSE:APOLLOHOSP26JUL7500CE"


def test_spacing_and_case_are_normalised() -> None:
    resolved = _name_resolver().resolve("  coforge ", 1500, "CE", on_date=ON)
    assert resolved.tradingsymbol == "NSE:COFORGE26JUL1500CE"


def test_an_ambiguous_name_is_refused_rather_than_guessed() -> None:
    # A bare "Apollo" fits both APOLLOHOSP and APOLLOTYRE. Buying the wrong
    # company is far worse than reporting that the name was unclear.
    with pytest.raises(InstrumentNotFoundError, match="Ambiguous"):
        _name_resolver().resolve("Apollo", 7500, "CE", on_date=ON)


def test_an_unknown_name_is_reported_as_not_in_the_master() -> None:
    with pytest.raises(InstrumentNotFoundError, match="Unknown underlying"):
        _name_resolver().resolve("Notacompany", 100, "CE", on_date=ON)


def test_lot_size_comes_from_the_master() -> None:
    assert _name_resolver().lot_size_for("COFORGE", on_date=ON) == 150


def test_lot_size_accepts_a_spoken_name_too() -> None:
    assert _name_resolver().lot_size_for("Apollo hospital", on_date=ON) == 125


def test_lot_size_is_none_for_an_unknown_underlying() -> None:
    # None, not an exception: the provider falls back and the engine rejects
    # cleanly rather than the listener crashing on a stray message.
    assert _name_resolver().lot_size_for("Notacompany", on_date=ON) is None


def test_lot_size_is_none_for_an_ambiguous_name() -> None:
    assert _name_resolver().lot_size_for("Apollo", on_date=ON) is None


def test_an_exact_ticker_wins_over_a_longer_one() -> None:
    # An exact match must short-circuit: a ticker that happens to be a prefix of
    # another ("APOLLOTYRE" vs a hypothetical longer name) is not ambiguous when
    # the signal names it exactly.
    assert _name_resolver().lot_size_for("APOLLOTYRE", on_date=ON) == 1700


ALIAS_ROWS: list[dict[str, Any]] = [
    _row("BAJFINANCE", "CE", 7000, "NSE:BAJFINANCE26JUL7000CE", date(2026, 7, 31), lot_size=750),
    _row("ULTRACEMCO", "CE", 12000, "NSE:ULTRACEMCO26JUL12000CE", date(2026, 7, 31), lot_size=50),
    _row("LT", "CE", 3600, "NSE:LT26JUL3600CE", date(2026, 7, 31), lot_size=175),
    _row("SBIN", "CE", 800, "NSE:SBIN26JUL800CE", date(2026, 7, 31), lot_size=750),
    _row("SBICARD", "CE", 800, "NSE:SBICARD26JUL800CE", date(2026, 7, 31), lot_size=800),
    _row("SBILIFE", "CE", 1800, "NSE:SBILIFE26JUL1800CE", date(2026, 7, 31), lot_size=375),
]


def _alias_resolver() -> FyersInstrumentResolver:
    return FyersInstrumentResolver(FakeSource(ALIAS_ROWS))


@pytest.mark.parametrize(
    ("spoken", "lot_size"),
    [
        ("Bajaj finance", 750),   # ticker drops interior letters
        ("Ultratech", 50),        # ticker renames outright
        ("L&T", 175),             # punctuation stripped to an exact ticker
        ("l & t", 175),
        ("Sbi", 750),             # short form that is otherwise ambiguous
    ],
)
def test_spoken_names_resolve_through_aliases(spoken: str, lot_size: int) -> None:
    # Prefix matching alone cannot bridge these, and each is a name a real signal
    # actually uses.
    assert _alias_resolver().lot_size_for(spoken, on_date=ON) == lot_size


def test_an_alias_beats_the_ambiguity_it_would_otherwise_cause() -> None:
    # "SBI" prefixes SBIN, SBICARD and SBILIFE; a trader saying it means the bank.
    assert _alias_resolver().resolve("SBI", 800, "CE", on_date=ON).tradingsymbol == (
        "NSE:SBIN26JUL800CE"
    )


def test_exchange_comes_from_the_symbol_not_the_numeric_column() -> None:
    # The real master stores the exchange as a numeric code ("10"), which is
    # meaningless in a log line or an alert.
    resolver = FyersInstrumentResolver(
        FakeSource(
            [_row("COFORGE", "CE", 1500, "NSE:COFORGE26JUL1500CE", date(2026, 7, 31), exchange="10")]
        )
    )
    assert resolver.resolve("COFORGE", 1500, "CE", on_date=ON).exchange == "NSE"
