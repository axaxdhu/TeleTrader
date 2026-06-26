"""Lot-size resolution for index options.

Options are traded in *lots*; the broker order needs the actual unit count,
which is ``lots × lot_size`` for the underlying. Lot sizes are set by the
exchange (NSE) and revised periodically, and they differ per underlying
(NIFTY ≠ BANKNIFTY), so they must never be hardcoded.

This module defines the :class:`LotSizeProvider` seam the trade engine depends
on, plus a config-backed implementation for now.

.. note::

    **Future (Phase 5 — broker integration):** add a ``BrokerLotSizeProvider``
    here that reads lot sizes straight from the broker's instrument master
    instead of ``.env`` — e.g. Zerodha Kite's ``kite.instruments("NFO")`` dump
    (each row carries a ``lot_size`` field) or the FYERS symbol-master files.
    Fetch once on startup, cache, and refresh daily. It only needs to implement
    :class:`LotSizeProvider`, so the engine and all rules stay untouched; just
    swap which provider ``main.py`` injects. Keep :class:`ConfigLotSizeProvider`
    as a fallback for when the instrument fetch is unavailable.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = ["ConfigLotSizeProvider", "LotSizeProvider"]


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
