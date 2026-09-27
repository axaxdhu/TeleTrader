"""Lot-size resolution for option contracts.

Options are traded in *lots*; the broker order needs the actual unit count,
which is ``lots × lot_size`` for the underlying. Lot sizes are set by the
exchange (NSE) and revised periodically, and they differ per underlying
(NIFTY ≠ BANKNIFTY), so they must never be hardcoded.

This module defines the :class:`LotSizeProvider` seam the trade engine depends
on, plus a config-backed implementation for now.

.. note::

    :class:`BrokerLotSizeProvider` (below) reads lot sizes straight from the
    broker's instrument master instead of ``.env``. This became necessary — not
    merely nicer — once a channel started trading **stock** options: ``LOT_SIZES``
    only ever listed the index underlyings, so every stock signal was rejected as
    unsized. :class:`ConfigLotSizeProvider` remains the fallback for when the
    master cannot be reached.

"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone, tzinfo
from typing import Callable, Protocol, runtime_checkable

from .logging_config import get_logger

__all__ = ["BrokerLotSizeProvider", "ConfigLotSizeProvider", "LotSizeProvider"]


@runtime_checkable
class LotSizeProvider(Protocol):
    """Resolves the exchange lot size for an underlying.

    The trade engine depends on this interface, not on any concrete source, so
    the source can change (config now, broker later) without touching the engine.
    """

    def lot_size_for(self, underlying: str) -> int | None:
        """Return the lot size for ``underlying``, or ``None`` if unknown."""
        ...


@dataclass(frozen=True, slots=True)
class ConfigLotSizeProvider:
    """Serves lot sizes from a static, config-supplied mapping.

    Lookup is case-insensitive on the underlying. An unknown underlying returns
    ``None`` (the engine treats that as a clean rejection rather than guessing).
    """

    lot_sizes: Mapping[str, int]

    def lot_size_for(self, underlying: str) -> int | None:
        return self.lot_sizes.get(underlying.upper())


logger = get_logger(__name__)


@runtime_checkable
class MasterLotSizeSource(Protocol):
    """A broker instrument master that can report an underlying's lot size.

    Satisfied by the FYERS and Kite instrument resolvers, which already index the
    master to resolve tradingsymbols — the lot size is on the same rows.
    """

    def lot_size_for(self, underlying: str, *, on_date: date) -> int | None:
        """Return the lot size for ``underlying`` as of ``on_date``, or ``None``."""
        ...


class BrokerLotSizeProvider:
    """Serves lot sizes from the broker's instrument master, with a fallback.

    The master is the exchange's own answer, so it covers every tradable
    underlying (stock options included) and stays correct across the periodic
    revisions that make a hand-kept ``LOT_SIZES`` list go stale.

    Because the master is fetched over the network, a lookup that fails for any
    reason falls back to the configured provider rather than taking the engine
    down; if neither knows the underlying the result is ``None``, which the engine
    treats as a clean rejection instead of guessing a size.
    """

    def __init__(
        self,
        source: MasterLotSizeSource,
        *,
        fallback: LotSizeProvider | None = None,
        tz: tzinfo = timezone.utc,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._source = source
        self._fallback = fallback
        self._tz = tz
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def lot_size_for(self, underlying: str) -> int | None:
        on_date = self._clock().astimezone(self._tz).date()
        try:
            lot_size = self._source.lot_size_for(underlying, on_date=on_date)
        except Exception as exc:  # noqa: BLE001 — a master fetch must not break sizing
            logger.warning(
                "Lot size lookup failed for %s (%s); falling back to config.",
                underlying,
                exc,
            )
            lot_size = None
        if lot_size:
            return lot_size
        if self._fallback is None:
            return None
        fallback_size = self._fallback.lot_size_for(underlying)
        if fallback_size:
            logger.info(
                "Lot size for %s not in the broker master; using configured %s.",
                underlying,
                fallback_size,
            )
        return fallback_size
