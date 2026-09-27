"""Unit tests for lot-size resolution, config-backed and broker-backed.

Sizing is where a stock-option channel meets reality: ``LOT_SIZES`` only ever
listed the index underlyings, so every stock signal was rejected as unsized until
the broker's instrument master became the source. These tests cover that
substitution and — just as important — its failure modes, because a wrong lot
size is a wrong order size.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

import pytest

from teletrader.lot_size import (
    BrokerLotSizeProvider,
    ConfigLotSizeProvider,
    LotSizeProvider,
)

NOW = datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)


class FakeMaster:
    """An instrument master that knows a few underlyings (or fails)."""

    def __init__(
        self,
        sizes: dict[str, int] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self._sizes = sizes or {}
        self._error = error
        self.calls: list[tuple[str, date]] = []

    def lot_size_for(self, underlying: str, *, on_date: date) -> int | None:
        self.calls.append((underlying, on_date))
        if self._error is not None:
            raise self._error
        return self._sizes.get(underlying.upper())


def _provider(
    master: FakeMaster, *, fallback: LotSizeProvider | None = None
) -> BrokerLotSizeProvider:
    return BrokerLotSizeProvider(
        master, fallback=fallback, tz=timezone.utc, clock=lambda: NOW
    )


# --- Config-backed ------------------------------------------------------------


def test_config_provider_is_case_insensitive() -> None:
    provider = ConfigLotSizeProvider({"NIFTY": 65})
    assert provider.lot_size_for("nifty") == 65


def test_config_provider_returns_none_for_unknown() -> None:
    # The engine treats None as a clean rejection; guessing a size would be worse.
    assert ConfigLotSizeProvider({"NIFTY": 65}).lot_size_for("COFORGE") is None


# --- Broker-backed ------------------------------------------------------------


def test_stock_underlying_is_sized_from_the_master() -> None:
    # The case the whole change exists for: a stock option that no configured
    # list covers.
    provider = _provider(FakeMaster({"COFORGE": 150}))
    assert provider.lot_size_for("COFORGE") == 150


def test_lookup_uses_the_current_trading_date() -> None:
    master = FakeMaster({"COFORGE": 150})
    _provider(master).lot_size_for("COFORGE")
    assert master.calls == [("COFORGE", date(2026, 9, 28))]


def test_falls_back_to_config_when_the_master_does_not_know() -> None:
    provider = _provider(
        FakeMaster({}), fallback=ConfigLotSizeProvider({"NIFTY": 65})
    )
    assert provider.lot_size_for("NIFTY") == 65


def test_falls_back_to_config_when_the_master_fetch_fails() -> None:
    # The master is fetched over the network; a failure must not take sizing —
    # and therefore the whole channel — down.
    provider = _provider(
        FakeMaster(error=RuntimeError("network down")),
        fallback=ConfigLotSizeProvider({"NIFTY": 65}),
    )
    assert provider.lot_size_for("NIFTY") == 65


def test_a_failed_fetch_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    provider = _provider(FakeMaster(error=RuntimeError("network down")))
    with caplog.at_level(logging.WARNING):
        provider.lot_size_for("NIFTY")
    assert "network down" in caplog.text


def test_unknown_everywhere_is_none() -> None:
    provider = _provider(FakeMaster({}), fallback=ConfigLotSizeProvider({}))
    assert provider.lot_size_for("WHATEVER") is None


def test_unknown_with_no_fallback_is_none() -> None:
    assert _provider(FakeMaster({})).lot_size_for("WHATEVER") is None


def test_master_wins_over_a_stale_configured_size() -> None:
    # Exchange lot sizes are revised periodically; the master is the live answer
    # and a leftover .env value must not override it.
    provider = _provider(
        FakeMaster({"NIFTY": 75}), fallback=ConfigLotSizeProvider({"NIFTY": 65})
    )
    assert provider.lot_size_for("NIFTY") == 75


def test_satisfies_the_lot_size_provider_protocol() -> None:
    assert isinstance(_provider(FakeMaster({})), LotSizeProvider)
