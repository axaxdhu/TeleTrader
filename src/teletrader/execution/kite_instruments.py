"""Resolve a broker-neutral option into its exact Zerodha Kite tradingsymbol.

A signal names an option only by *underlying, strike, and type* (e.g. NIFTY 23900
PE) — it carries no expiry. Kite's ``place_order`` needs the precise
``tradingsymbol`` (which encodes the expiry, e.g. ``NIFTY2516823900PE``). This
module bridges that gap using Kite's instrument master.

Strategy:

* Fetch the NFO instrument dump (``kite.instruments("NFO")``) — several MB — at
  most **once per trading day** and cache it in memory; rebuild only when the
  order date rolls over to a new day.
* Index the option contracts by ``(name, instrument_type, strike)`` and, for a
  lookup, pick the **nearest expiry on or after the order date** — i.e. the
  current weekly (or the nearest monthly when an underlying has no weeklies). The
  available expiries come straight from the master, so we never hardcode the
  exchange's weekly/monthly rules (which change).

This is the *only* place option-symbol resolution lives; it sits inside the Kite
execution layer and is injected into :class:`~teletrader.execution.kite.KiteExecutor`
(a fake is injected in tests, so no network is ever touched).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol, runtime_checkable

from ..logging_config import get_logger
from .exceptions import InstrumentNotFoundError

__all__ = [
    "InstrumentResolver",
    "InstrumentSource",
    "KiteInstrumentResolver",
    "ResolvedInstrument",
]

logger = get_logger(__name__)

_NFO = "NFO"
_OPTION_TYPES = frozenset({"CE", "PE"})


@dataclass(frozen=True, slots=True)
class ResolvedInstrument:
    """A concrete broker contract resolved from a neutral option description."""

    tradingsymbol: str
    exchange: str
    expiry: date
    lot_size: int


@runtime_checkable
class InstrumentSource(Protocol):
    """The slice of the Kite client the resolver needs: the instrument dump."""

    def instruments(self, exchange: str) -> list[dict[str, Any]]:  # pragma: no cover
        """Return the broker's instrument master for ``exchange``."""


@runtime_checkable
class InstrumentResolver(Protocol):
    """Resolves a neutral option into a concrete broker contract."""

    def resolve(
        self, underlying: str, strike: int, option_type: str, *, on_date: date
    ) -> ResolvedInstrument:  # pragma: no cover - interface
        ...


class KiteInstrumentResolver:
    """Resolves index-option contracts from Kite's NFO instrument master.

    The dump is fetched (and indexed) lazily on first use and cached for the
    trading day; a different ``on_date`` triggers a refresh. The instrument source
    (the Kite client) is injected, so tests pass a fake and never hit the network.
    """

    def __init__(self, source: InstrumentSource) -> None:
        self._source = source
        self._cached_on: date | None = None
        self._index: dict[tuple[str, str, int], list[ResolvedInstrument]] = {}

    def resolve(
        self, underlying: str, strike: int, option_type: str, *, on_date: date
    ) -> ResolvedInstrument:
        """Return the nearest-weekly contract for the option, or raise.

        Picks the contract whose expiry is the earliest on or after ``on_date``
        (the current weekly). Raises :class:`InstrumentNotFoundError` if no such
        contract exists in the master.
        """
        self._ensure_index(on_date)
        key = (underlying.upper(), option_type.upper(), int(strike))
        upcoming = [c for c in self._index.get(key, ()) if c.expiry >= on_date]
        if not upcoming:
            raise InstrumentNotFoundError(
                f"No Kite contract for {underlying} {strike} {option_type} "
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
        rows = self._source.instruments(_NFO)
        index: dict[tuple[str, str, int], list[ResolvedInstrument]] = {}
        for row in rows:
            instrument = _row_to_option(row)
            if instrument is None:
                continue
            key, resolved = instrument
            index.setdefault(key, []).append(resolved)
        for contracts in index.values():
            contracts.sort(key=lambda c: c.expiry)
        self._index = index
        self._cached_on = on_date
        logger.info(
            "Loaded %s NFO option contracts from Kite (cached for %s).",
            sum(len(v) for v in index.values()),
            on_date.isoformat(),
        )


def _row_to_option(
    row: dict[str, Any],
) -> tuple[tuple[str, str, int], ResolvedInstrument] | None:
    """Map one instrument-master row to an index key + contract, or ``None``.

    Skips anything that is not a well-formed CE/PE option row (futures, rows with
    a missing/odd strike or expiry), so a single bad row never breaks the load.
    """
    option_type = row.get("instrument_type")
    if option_type not in _OPTION_TYPES:
        return None
    expiry = _as_date(row.get("expiry"))
    if expiry is None:
        return None
    try:
        name = str(row["name"]).upper()
        strike = int(float(row["strike"]))
        tradingsymbol = str(row["tradingsymbol"])
    except (KeyError, TypeError, ValueError):
        return None
    resolved = ResolvedInstrument(
        tradingsymbol=tradingsymbol,
        exchange=str(row.get("exchange") or _NFO),
        expiry=expiry,
        lot_size=int(row.get("lot_size") or 0),
    )
    return (name, option_type, strike), resolved


def _as_date(value: Any) -> date | None:
    """Coerce an instrument-master expiry to a plain :class:`date`, or ``None``.

    Kite parses ``expiry`` to a ``date``; a ``datetime`` (or empty value) is
    normalised/skipped so comparisons stay date-to-date.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None
