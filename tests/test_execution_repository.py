"""Unit tests for the execution-history persistence layer."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from teletrader.database import SCHEMA_VERSION, connect, initialize
from teletrader.execution import (
    ExecutionRepository,
    ExecutionResult,
    ExecutionStatus,
    OrderRequest,
    OrderType,
    TransactionType,
)

NOW = datetime(2026, 6, 29, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def connection() -> sqlite3.Connection:
    conn = connect(":memory:")
    initialize(conn)
    yield conn
    conn.close()


@pytest.fixture
def repo(connection: sqlite3.Connection) -> ExecutionRepository:
    return ExecutionRepository(connection)


def _order(**overrides: object) -> OrderRequest:
    defaults = dict(
        symbol="NIFTY24JUN23900PE",
        transaction_type=TransactionType.BUY,
        quantity=65,
        order_type=OrderType.MARKET,
        entry_price=165.0,
        stop_loss=150.0,
        target=198.0,
        signal_id=None,
    )
    defaults.update(overrides)
    return OrderRequest(**defaults)  # type: ignore[arg-type]


def _result(status: ExecutionStatus = ExecutionStatus.SUCCESS, **order_kw: object) -> ExecutionResult:
    return ExecutionResult(
        status=status, order=_order(**order_kw), remarks="ok", timestamp=NOW
    )


# --- Schema -------------------------------------------------------------------


def test_executions_table_exists(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='executions'"
    ).fetchone()
    assert row is not None


def test_paper_tables_were_dropped(connection: sqlite3.Connection) -> None:
    # The pivot away from the simulated broker removed these.
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('paper_orders', 'paper_positions')"
    ).fetchall()
    assert rows == []


def test_schema_version_is_current(connection: sqlite3.Connection) -> None:
    assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


# --- Persistence --------------------------------------------------------------


def test_add_persists_and_returns_row(repo: ExecutionRepository) -> None:
    stored = repo.add(_result())
    assert stored.id == 1
    assert stored.symbol == "NIFTY24JUN23900PE"
    assert stored.action is TransactionType.BUY
    assert stored.status is ExecutionStatus.SUCCESS
    assert stored.timestamp == NOW


def test_roundtrip_all_fields(repo: ExecutionRepository) -> None:
    stored = repo.add(_result(ExecutionStatus.REJECTED))
    fetched = repo.get(stored.id)
    assert fetched is not None
    assert fetched.status is ExecutionStatus.REJECTED
    assert fetched.entry_price == 165.0
    assert fetched.stop_loss == 150.0
    assert fetched.order_type is OrderType.MARKET


def test_count_and_list_all(repo: ExecutionRepository) -> None:
    assert repo.count() == 0
    repo.add(_result())
    repo.add(_result(ExecutionStatus.REJECTED))
    assert repo.count() == 2
    assert [e.status for e in repo.list_all()] == [
        ExecutionStatus.SUCCESS,
        ExecutionStatus.REJECTED,
    ]


def test_list_for_signal_filters_by_signal_id(
    repo: ExecutionRepository, connection: sqlite3.Connection
) -> None:
    # signal_id is a FK to signals(id); insert two signals to satisfy it.
    connection.execute(
        "INSERT INTO signals (message_hash, underlying, strike, option_type, action,"
        " entry_price, stop_loss, target, target_open_ended, raw_text, created_at, trade_date)"
        " VALUES ('h1','NIFTY',23900,'PE','BUY',165,150,198,1,'x','2026-06-29T00:00:00+00:00','2026-06-29'),"
        "        ('h2','NIFTY',24000,'PE','BUY',165,150,198,1,'y','2026-06-29T00:00:00+00:00','2026-06-29')"
    )
    connection.commit()
    repo.add(_result(signal_id=1))
    repo.add(_result(signal_id=1))
    repo.add(_result(signal_id=2))
    assert len(repo.list_for_signal(1)) == 2
    assert len(repo.list_for_signal(2)) == 1


def test_get_missing_returns_none(repo: ExecutionRepository) -> None:
    assert repo.get(999) is None
