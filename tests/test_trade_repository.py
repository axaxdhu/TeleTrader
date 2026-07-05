"""Unit tests for the active-trade persistence layer (trades table)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from teletrader.database import SCHEMA_VERSION, connect, initialize
from teletrader.parser import OptionType, parse_signal
from teletrader.repository import SignalRepository
from teletrader.trade_repository import StoredTrade, TradeRepository, TradeStatus


# --- Fixtures -----------------------------------------------------------------

@pytest.fixture
def connection() -> sqlite3.Connection:
    """An initialised in-memory database, fresh per test."""
    conn = connect(":memory:")
    initialize(conn)
    yield conn
    conn.close()


@pytest.fixture
def repo(connection: sqlite3.Connection) -> TradeRepository:
    return TradeRepository(connection)


def _open(repo: TradeRepository, **overrides: object) -> StoredTrade:
    """Open a trade with sensible defaults, overriding fields as needed."""
    defaults = dict(
        signal_id=None,
        underlying="NIFTY",
        strike=23900,
        option_type=OptionType.PUT,
        quantity=65,
    )
    defaults.update(overrides)
    return repo.open_trade(**defaults)  # type: ignore[arg-type]


# --- Schema / migration -------------------------------------------------------

def test_migration_creates_trades_table(connection: sqlite3.Connection) -> None:
    assert SCHEMA_VERSION >= 7
    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='trades'"
    ).fetchone()
    assert row is not None


def test_initialize_is_idempotent(connection: sqlite3.Connection) -> None:
    # Re-running initialize must not error or change the version.
    assert initialize(connection) == SCHEMA_VERSION


# --- open_trade ---------------------------------------------------------------

def test_open_trade_defaults_to_pending_entry(repo: TradeRepository) -> None:
    trade = _open(repo)
    assert trade.id > 0
    assert trade.status is TradeStatus.PENDING_ENTRY
    assert trade.is_active
    assert trade.option_type is OptionType.PUT
    assert trade.quantity == 65
    # Broker handles are unset until a live executor fills them in.
    assert trade.tradingsymbol is None
    assert trade.entry_order_id is None


def test_open_trade_persists_levels_and_handles(
    repo: TradeRepository, connection: sqlite3.Connection
) -> None:
    # A real signal row to satisfy the trades.signal_id foreign key.
    stored_signal = SignalRepository(connection).add(
        parse_signal("NIFTY 23900 PE ABOVE 165\n\nSL-150\n\nTGT-200+")
    )
    trade = _open(
        repo,
        signal_id=stored_signal.id,
        entry_price=165.0,
        stop_loss=150.0,
        target=198.0,
        tradingsymbol="NIFTY2570323900PE",
        entry_order_id="OID1",
    )
    fetched = repo.get(trade.id)
    assert fetched == trade
    assert fetched.signal_id == stored_signal.id
    assert fetched.entry_price == 165.0
    assert fetched.stop_loss == 150.0
    assert fetched.tradingsymbol == "NIFTY2570323900PE"
    assert fetched.entry_order_id == "OID1"


def test_get_missing_returns_none(repo: TradeRepository) -> None:
    assert repo.get(999) is None


# --- most_recent_active -------------------------------------------------------

def test_most_recent_active_returns_latest(repo: TradeRepository) -> None:
    _open(repo, strike=23900)
    second = _open(repo, strike=24000)
    active = repo.most_recent_active()
    assert active is not None
    assert active.id == second.id
    assert active.strike == 24000


def test_most_recent_active_ignores_closed_and_cancelled(repo: TradeRepository) -> None:
    first = _open(repo, strike=23900)
    second = _open(repo, strike=24000)
    # Close the newest; the active one should fall back to the older open trade.
    repo.update_status(second.id, TradeStatus.CLOSED)
    active = repo.most_recent_active()
    assert active is not None and active.id == first.id

    repo.update_status(first.id, TradeStatus.CANCELLED)
    assert repo.most_recent_active() is None


def test_most_recent_active_none_when_empty(repo: TradeRepository) -> None:
    assert repo.most_recent_active() is None


def test_open_status_counts_as_active(repo: TradeRepository) -> None:
    trade = _open(repo)
    repo.update_status(trade.id, TradeStatus.OPEN)
    active = repo.most_recent_active()
    assert active is not None and active.id == trade.id
    assert active.status is TradeStatus.OPEN


def test_list_active_newest_first(repo: TradeRepository) -> None:
    a = _open(repo)
    b = _open(repo)
    c = _open(repo)
    repo.update_status(b.id, TradeStatus.CLOSED)
    ids = [t.id for t in repo.list_active()]
    assert ids == [c.id, a.id]


# --- mutators -----------------------------------------------------------------

def test_update_stop_loss_persists_and_touches(repo: TradeRepository) -> None:
    trade = _open(repo, stop_loss=150.0, created_at=datetime(2026, 6, 30, tzinfo=timezone.utc))
    later = datetime(2026, 6, 30, 10, tzinfo=timezone.utc)
    repo.update_stop_loss(trade.id, 165.0, when=later)
    updated = repo.get(trade.id)
    assert updated.stop_loss == 165.0
    assert updated.updated_at == later
    assert updated.created_at == trade.created_at  # unchanged


def test_update_target_persists(repo: TradeRepository) -> None:
    trade = _open(repo, target=198.0)
    repo.update_target(trade.id, 250.0)
    assert repo.get(trade.id).target == 250.0


def test_set_broker_handles_is_incremental(repo: TradeRepository) -> None:
    trade = _open(repo)
    repo.set_broker_handles(trade.id, entry_order_id="OID1")
    repo.set_broker_handles(
        trade.id, sl_order_id="SL1", entry_price=164.5, tradingsymbol="SYM"
    )
    updated = repo.get(trade.id)
    assert updated.entry_order_id == "OID1"   # preserved across the second call
    assert updated.sl_order_id == "SL1"
    assert updated.entry_price == 164.5
    assert updated.tradingsymbol == "SYM"
    assert updated.target_order_id is None


def test_set_broker_handles_noop_when_nothing_given(repo: TradeRepository) -> None:
    trade = _open(repo, entry_order_id="OID1")
    repo.set_broker_handles(trade.id)  # nothing to write
    assert repo.get(trade.id).entry_order_id == "OID1"
