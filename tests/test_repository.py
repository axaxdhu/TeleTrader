"""Unit tests for the SQLite persistence layer (database + repository)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from teletrader.database import SCHEMA_VERSION, connect, initialize
from teletrader.parser import Action, OptionType, Signal, parse_signal
from teletrader.repository import (
    DuplicateSignalError,
    SignalRepository,
    signal_hash,
)


# --- Fixtures -----------------------------------------------------------------

@pytest.fixture
def connection() -> sqlite3.Connection:
    """An initialised in-memory database, fresh per test."""
    conn = connect(":memory:")
    initialize(conn)
    yield conn
    conn.close()


@pytest.fixture
def repo(connection: sqlite3.Connection) -> SignalRepository:
    return SignalRepository(connection)


def _signal(**overrides: object) -> Signal:
    """Build a Signal, overriding individual fields as needed."""
    defaults = dict(
        underlying="NIFTY",
        strike=23900,
        option_type=OptionType.PUT,
        action=Action.BUY,
        entry_price=165.0,
        stop_loss=150.0,
        target=200.0,
        target_open_ended=True,
        raw_text="NIFTY 23900 PE ABOVE 165\n\nSL-150\n\nTGT-200+",
    )
    defaults.update(overrides)
    return Signal(**defaults)  # type: ignore[arg-type]


# --- Schema / migrations ------------------------------------------------------

def test_initialize_sets_schema_version(connection: sqlite3.Connection) -> None:
    version = connection.execute("PRAGMA user_version;").fetchone()[0]
    assert version == SCHEMA_VERSION


def test_initialize_creates_signals_table(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='signals'"
    ).fetchone()
    assert row is not None


def test_initialize_is_idempotent(connection: sqlite3.Connection) -> None:
    # Running again must not raise (e.g. "table already exists") or re-migrate.
    assert initialize(connection) == SCHEMA_VERSION
    assert initialize(connection) == SCHEMA_VERSION


# --- Storing signals ----------------------------------------------------------

def test_add_returns_stored_signal_with_id(repo: SignalRepository) -> None:
    stored = repo.add(_signal())
    assert stored.id == 1
    assert stored.signal == _signal()
    assert stored.message_hash == signal_hash(_signal())
    assert stored.created_at.tzinfo is timezone.utc


def test_add_persists_all_fields(repo: SignalRepository) -> None:
    stored = repo.add(_signal())
    fetched = repo.get(stored.id)
    assert fetched is not None
    assert fetched.signal == _signal()
    assert fetched.message_hash == stored.message_hash


def test_count_and_list_all(repo: SignalRepository) -> None:
    assert repo.count() == 0
    repo.add(_signal())
    repo.add(_signal(strike=24000))
    assert repo.count() == 2
    assert [s.signal.strike for s in repo.list_all()] == [23900, 24000]


def test_get_missing_returns_none(repo: SignalRepository) -> None:
    assert repo.get(999) is None


def test_created_at_roundtrips(repo: SignalRepository) -> None:
    stored = repo.add(_signal())
    fetched = repo.get(stored.id)
    assert fetched is not None
    assert isinstance(fetched.created_at, datetime)
    assert fetched.created_at == stored.created_at


# --- Duplicate detection ------------------------------------------------------

def test_duplicate_signal_is_rejected(repo: SignalRepository) -> None:
    repo.add(_signal())
    with pytest.raises(DuplicateSignalError):
        repo.add(_signal())
    assert repo.count() == 1  # the duplicate was not stored


def test_duplicate_error_carries_hash(repo: SignalRepository) -> None:
    repo.add(_signal())
    with pytest.raises(DuplicateSignalError) as exc_info:
        repo.add(_signal())
    assert exc_info.value.message_hash == signal_hash(_signal())


def test_same_content_different_whitespace_is_duplicate(repo: SignalRepository) -> None:
    # Two raw messages that parse to the same trade must collide.
    a = parse_signal("NIFTY 23900 PE ABOVE 165\n\nSL-150\n\nTGT-200+")
    b = parse_signal("nifty  23900  pe  above  165\nSL - 150\nTGT - 200 +")
    assert a is not None and b is not None
    assert a.raw_text != b.raw_text  # genuinely different source text
    repo.add(a)
    with pytest.raises(DuplicateSignalError):
        repo.add(b)


def test_same_signal_same_day_is_rejected(repo: SignalRepository) -> None:
    day = datetime(2026, 6, 26, 10, 0, tzinfo=timezone.utc)
    repo.add(_signal(), created_at=day)
    with pytest.raises(DuplicateSignalError):
        repo.add(_signal(), created_at=day.replace(hour=14))  # later, same day
    assert repo.count() == 1


def test_same_signal_different_day_is_accepted(repo: SignalRepository) -> None:
    day1 = datetime(2026, 6, 26, 10, 0, tzinfo=timezone.utc)
    day2 = datetime(2026, 6, 29, 10, 0, tzinfo=timezone.utc)
    first = repo.add(_signal(), created_at=day1)
    second = repo.add(_signal(), created_at=day2)  # same content, next day
    assert repo.count() == 2
    assert first.id != second.id


def test_exists_is_day_scoped(repo: SignalRepository) -> None:
    from datetime import date

    day1 = datetime(2026, 6, 26, 10, 0, tzinfo=timezone.utc)
    repo.add(_signal(), created_at=day1)
    assert repo.exists(_signal(), on_date=date(2026, 6, 26)) is True
    assert repo.exists(_signal(), on_date=date(2026, 6, 27)) is False


def test_different_signals_are_not_duplicates(repo: SignalRepository) -> None:
    repo.add(_signal())
    repo.add(_signal(strike=24000))           # different strike
    repo.add(_signal(option_type=OptionType.CALL))  # different right
    repo.add(_signal(target=250.0))           # different target
    assert repo.count() == 4


def test_exists(repo: SignalRepository) -> None:
    assert repo.exists(_signal()) is False
    repo.add(_signal())
    assert repo.exists(_signal()) is True
    assert repo.exists(_signal(strike=24000)) is False


# --- Hash determinism ---------------------------------------------------------

def test_signal_hash_is_deterministic() -> None:
    assert signal_hash(_signal()) == signal_hash(_signal())


def test_signal_hash_ignores_raw_text() -> None:
    assert signal_hash(_signal(raw_text="something else")) == signal_hash(_signal())


def test_signal_hash_ignores_int_vs_float_prices() -> None:
    assert signal_hash(_signal(entry_price=165)) == signal_hash(_signal(entry_price=165.0))


def test_signal_hash_changes_with_content() -> None:
    assert signal_hash(_signal()) != signal_hash(_signal(strike=24000))
    assert signal_hash(_signal()) != signal_hash(_signal(target_open_ended=False))
