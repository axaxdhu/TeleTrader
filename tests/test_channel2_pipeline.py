"""Unit tests for the channel-2 parse-only pipeline and source-scoped storage.

Channel 2 parses + stores but does not trade, and its signals are isolated from
channel 1's via the repository's ``source`` scoping (so, e.g., the trade engine's
counts never see channel-2 rows).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from teletrader.database import connect, initialize
from teletrader.pipeline import Channel2Pipeline, PipelineStatus
from teletrader.repository import SignalRepository

WHEN = datetime(2026, 7, 3, 4, 30, tzinfo=timezone.utc)
C2_SIGNAL = "Nifty 23900 pe above 128\n\nLot 65\n\nTarget 140, 155, 175++\n\nSl 115\n\nCmp 124"


@pytest.fixture
def connection() -> sqlite3.Connection:
    conn = connect(":memory:")
    initialize(conn)
    yield conn
    conn.close()


def _pipeline(connection: sqlite3.Connection) -> Channel2Pipeline:
    return Channel2Pipeline(
        SignalRepository(connection, tz=timezone.utc, source="channel2")
    )


def test_noise_is_ignored(connection: sqlite3.Connection) -> None:
    result = _pipeline(connection).process("sl hit on this", when=WHEN)
    assert result.status is PipelineStatus.IGNORED


def test_valid_signal_is_stored(connection: sqlite3.Connection) -> None:
    result = _pipeline(connection).process(C2_SIGNAL, when=WHEN)
    assert result.status is PipelineStatus.STORED
    assert result.stored_id is not None
    assert result.signal is not None
    assert result.signal.target == 140.0  # first target, raw

    stored = SignalRepository(connection, source="channel2").list_all()
    assert len(stored) == 1
    assert stored[0].source == "channel2"


def test_same_day_repeat_is_duplicate(connection: sqlite3.Connection) -> None:
    pipe = _pipeline(connection)
    assert pipe.process(C2_SIGNAL, when=WHEN).status is PipelineStatus.STORED
    assert pipe.process(C2_SIGNAL, when=WHEN).status is PipelineStatus.DUPLICATE


def test_sources_are_isolated(connection: sqlite3.Connection) -> None:
    """Channel-2 rows must not appear in a channel-1-scoped repository, and
    each channel dedupes within itself."""
    ch2 = SignalRepository(connection, tz=timezone.utc, source="channel2")
    ch1 = SignalRepository(connection, tz=timezone.utc, source="channel1")

    Channel2Pipeline(ch2).process(C2_SIGNAL, when=WHEN)

    assert ch2.count() == 1
    assert ch1.count() == 0  # channel 1 sees none of channel 2's storage
    # And channel-1 counting (used by the engine's daily limit) excludes ch2.
    assert ch1.count_since(WHEN.replace(hour=0, minute=0)) == 0
