"""Resolve a broker-neutral option into its exact FYERS symbol ticker.

The FYERS analogue of :mod:`teletrader.execution.kite_instruments`. A signal names
an option only by *underlying, strike, and type* (e.g. NIFTY 23900 PE) with no
expiry; FYERS ``place_order`` needs the precise symbol string (which encodes the
expiry, e.g. ``NSE:NIFTY2571023900PE``). This module bridges that gap using
FYERS's **symbol master**.

Unlike Kite (which exposes the instrument dump via an API call), FYERS publishes
its master as **downloadable CSV files** (e.g. ``NSE_FO.csv``). So the source here
downloads + parses that CSV instead of calling the SDK; everything downstream
(the nearest-expiry selection, the per-day cache, the resolver interface) mirrors
the Kite resolver so the two are interchangeable behind
:class:`~teletrader.execution.kite_instruments.InstrumentResolver`.

The **nearest expiry on/after the order date** rule works for both index options
(weekly expiries) and stock options (monthly expiries) with no special-casing:
the master only lists the expiries that exist, so picking the earliest upcoming
one naturally yields the current weekly for an index and the current monthly for a
stock.

Returning the master's *own* symbol string (rather than constructing it) sidesteps
FYERS's differing weekly/monthly symbol formats entirely — the exact ticker to
trade comes straight from the file.
"""

from __future__ import annotations

import csv
import io
import urllib.request
from datetime import date, datetime
from typing import Any, Callable, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from ..logging_config import get_logger
from .exceptions import InstrumentNotFoundError
from .kite_instruments import ResolvedInstrument

__all__ = [
    "FyersCsvSymbolMaster",
    "FyersInstrumentResolver",
    "FyersSymbolSource",
]

logger = get_logger(__name__)

_OPTION_TYPES = frozenset({"CE", "PE"})

#: FYERS publishes its NSE F&O symbol master here (NIFTY/BANKNIFTY/FINNIFTY and
#: NSE stock options all live in this file). BSE derivatives (SENSEX/BANKEX) have
#: their own file; add it if those underlyings are traded.
_DEFAULT_MASTER_URLS: tuple[str, ...] = (
    "https://public.fyers.in/sym_details/NSE_FO.csv",
)

#: Expiry epochs in the master are seconds since the epoch in IST.
_IST = ZoneInfo("Asia/Kolkata")

# --- FYERS symbol-master CSV column layout --------------------------------------
# The sym_details CSV has **no header row**; fields are positional. These indices
# are FYERS's documented layout, but the format is not versioned — VERIFY THESE
# AGAINST A LIVE DOWNLOAD before trading. Keeping them here (one place) makes a
# fix a one-line change; the resolver itself works off normalised dict rows and is
# unaffected by the exact layout.
_COL_LOT_SIZE = 3
_COL_EXPIRY_EPOCH = 8
_COL_SYMBOL_TICKER = 9
_COL_EXCHANGE = 10
_COL_UNDERLYING = 13
_COL_STRIKE = 15
_COL_OPTION_TYPE = 16
_MIN_COLUMNS = _COL_OPTION_TYPE + 1


@runtime_checkable
class FyersSymbolSource(Protocol):
    """Supplies normalised option rows from the FYERS symbol master.

    Each row is a dict with the keys the resolver indexes on:
    ``underlying``, ``option_type`` (CE/PE), ``strike`` (int), ``expiry``
    (:class:`datetime.date`), ``symbol`` (the full FYERS ticker), ``exchange``,
    and ``lot_size`` (int). Declaring the seam as a Protocol lets tests inject
    fixture rows without downloading or parsing any CSV.
    """

    def option_rows(self) -> list[dict[str, Any]]:  # pragma: no cover - interface
        """Return the current option contracts as normalised rows."""


class FyersInstrumentResolver:
    """Resolves option contracts from FYERS's symbol master.

    Mirrors :class:`~teletrader.execution.kite_instruments.KiteInstrumentResolver`:
    the master is fetched + indexed lazily on first use and cached for the trading
    day; a different ``on_date`` triggers a refresh. The symbol source is injected,
    so tests pass a fake and never hit the network.
    """

    def __init__(self, source: FyersSymbolSource) -> None:
        self._source = source
        self._cached_on: date | None = None
        self._index: dict[tuple[str, str, int], list[ResolvedInstrument]] = {}

    def resolve(
        self, underlying: str, strike: int, option_type: str, *, on_date: date
    ) -> ResolvedInstrument:
        """Return the nearest-expiry FYERS contract for the option, or raise.

        Picks the contract whose expiry is the earliest on or after ``on_date``.
        Raises :class:`InstrumentNotFoundError` if the master has no such contract.
        """
        self._ensure_index(on_date)
        key = (underlying.upper(), option_type.upper(), int(strike))
        upcoming = [c for c in self._index.get(key, ()) if c.expiry >= on_date]
        if not upcoming:
            raise InstrumentNotFoundError(
                f"No FYERS contract for {underlying} {strike} {option_type} "
                f"expiring on/after {on_date.isoformat()}"
            )
        nearest = min(upcoming, key=lambda c: c.expiry)
        logger.info(
            "Resolved %s %s %s -> %s (expiry %s, lot_size %s)",
            underlying,
            strike,
            option_type,
            nearest.tradingsymbol,
            nearest.expiry.isoformat(),
            nearest.lot_size,
        )
        return nearest

    def _ensure_index(self, on_date: date) -> None:
        """Build (or refresh) the contract index for ``on_date`` if needed."""
        if self._cached_on == on_date and self._index:
            return
        index: dict[tuple[str, str, int], list[ResolvedInstrument]] = {}
        for row in self._source.option_rows():
            entry = _row_to_option(row)
            if entry is None:
                continue
            key, resolved = entry
            index.setdefault(key, []).append(resolved)
        for contracts in index.values():
            contracts.sort(key=lambda c: c.expiry)
        self._index = index
        self._cached_on = on_date
        logger.info(
            "Loaded %s FYERS option contracts (cached for %s).",
            sum(len(v) for v in index.values()),
            on_date.isoformat(),
        )


def _row_to_option(
    row: dict[str, Any],
) -> tuple[tuple[str, str, int], ResolvedInstrument] | None:
    """Map one normalised master row to an index key + contract, or ``None``.

    Skips anything that is not a well-formed CE/PE option row, so a single bad row
    never breaks the load.
    """
    option_type = str(row.get("option_type", "")).upper()
    if option_type not in _OPTION_TYPES:
        return None
    expiry = _as_date(row.get("expiry"))
    if expiry is None:
        return None
    try:
        underlying = str(row["underlying"]).upper()
        strike = int(float(row["strike"]))
        symbol = str(row["symbol"])
    except (KeyError, TypeError, ValueError):
        return None
    if not underlying or not symbol:
        return None
    resolved = ResolvedInstrument(
        tradingsymbol=symbol,
        exchange=str(row.get("exchange") or "NSE"),
        expiry=expiry,
        lot_size=int(row.get("lot_size") or 0),
    )
    return (underlying, option_type, strike), resolved


def _as_date(value: Any) -> date | None:
    """Coerce a master expiry (epoch seconds, datetime, or date) to a ``date``."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        epoch = int(value)
    except (TypeError, ValueError):
        return None
    if epoch <= 0:
        return None
    return datetime.fromtimestamp(epoch, tz=_IST).date()


class FyersCsvSymbolMaster:
    """Downloads and parses FYERS's symbol-master CSV(s) into normalised rows.

    This is the only place the CSV's positional column layout is interpreted.
    Rows that don't parse cleanly are skipped (never fatal). The downloader is
    injectable so tests can supply CSV text without a network call; production
    fetches over HTTPS.
    """

    def __init__(
        self,
        urls: tuple[str, ...] = _DEFAULT_MASTER_URLS,
        *,
        downloader: Callable[[str], str] | None = None,
    ) -> None:
        self._urls = urls
        self._download = downloader or _http_get

    def option_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for url in self._urls:
            text = self._download(url)
            rows.extend(_parse_master_csv(text))
        return rows


def _parse_master_csv(text: str) -> list[dict[str, Any]]:
    """Parse FYERS symbol-master CSV text into normalised option rows."""
    rows: list[dict[str, Any]] = []
    for fields in csv.reader(io.StringIO(text)):
        if len(fields) < _MIN_COLUMNS:
            continue
        option_type = fields[_COL_OPTION_TYPE].strip().upper()
        if option_type not in _OPTION_TYPES:
            continue  # equity/futures/index rows — not tradeable options here
        rows.append(
            {
                "underlying": fields[_COL_UNDERLYING].strip(),
                "option_type": option_type,
                "strike": fields[_COL_STRIKE].strip(),
                "expiry": fields[_COL_EXPIRY_EPOCH].strip(),
                "symbol": fields[_COL_SYMBOL_TICKER].strip(),
                "exchange": fields[_COL_EXCHANGE].strip(),
                "lot_size": fields[_COL_LOT_SIZE].strip(),
            }
        )
    return rows


def _http_get(url: str) -> str:
    """Fetch ``url`` and return its body as text (production downloader)."""
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 - fixed https URL
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")
