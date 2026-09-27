"""Persistence layer for parsed signals.

Provides :class:`SignalRepository`, a thin repository over the ``signals`` table
that stores every parsed :class:`~teletrader.parser.Signal` and rejects
duplicates. Duplicate detection is keyed off a *deterministic content hash* of
the signal's structured fields (see :func:`signal_hash`) — so the same trade
re-posted with different whitespace, casing, or surrounding chatter is still
recognised as a duplicate, while genuinely different signals are not.

Scope: persistence only. No trading, no broker integration (Phase 3).
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone, tzinfo

from .logging_config import get_logger
from .parser import Action, OptionType, Signal

__all__ = [
    "DEFAULT_SOURCE",
    "DuplicateSignalError",
    "SignalRepository",
    "StoredSignal",
    "signal_hash",
]

logger = get_logger(__name__)

#: Default channel tag for signals (the original, single-channel source). A
#: repository can be bound to a different source (e.g. ``"channel2"``) to keep
#: each channel's storage, dedupe, and counting isolated from the others.
DEFAULT_SOURCE = "channel1"


class DuplicateSignalError(Exception):
    """Raised when a signal whose content hash already exists is stored.

    Carries the offending ``message_hash`` so callers can log/branch without
    re-hashing.
    """

    def __init__(self, message_hash: str) -> None:
        super().__init__(f"Signal already stored (hash={message_hash})")
        self.message_hash = message_hash


@dataclass(frozen=True, slots=True)
class StoredSignal:
    """A :class:`Signal` as persisted: the original signal plus row metadata."""

    id: int
    message_hash: str
    created_at: datetime
    signal: Signal
    source: str = DEFAULT_SOURCE


def signal_hash(signal: Signal) -> str:
    """Return a deterministic SHA-256 hex digest of a signal's content.

    The digest is computed over the *structured* fields (not ``raw_text``) in a
    fixed canonical order, so two messages that parse to the same trade collide
    regardless of whitespace, casing, or any non-signal text around them. This
    is the duplicate-detection key.
    """
    canonical = "|".join(
        (
            signal.underlying,
            str(signal.strike),
            signal.option_type.value,
            signal.action.value,
            _canonical_number(signal.entry_price),
            _canonical_number(signal.stop_loss),
            _canonical_number(signal.target),
            "1" if signal.target_open_ended else "0",
        )
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_number(value: float) -> str:
    """Render a price stably so ``165`` and ``165.0`` hash identically."""
    return str(int(value)) if value.is_integer() else str(value)


class SignalRepository:
    """Stores and retrieves parsed signals in SQLite.

    The repository does not own the connection's lifecycle — it is injected
    (dependency injection), so tests can pass an in-memory connection and the
    application can share one connection across components.

    Duplicate detection is **per trading day**: the same signal is a duplicate
    only if already stored on the same date, and is accepted again on a later
    day. The day is derived in ``tz`` (inject the market timezone so "day" means
    the trading day, not a UTC day); it defaults to UTC.

    The repository is **source-scoped**: it stores and queries only rows for its
    ``source`` channel, so each channel's storage, dedupe, and counting are
    isolated. Bind one repository per channel (``source="channel1"`` /
    ``"channel2"``); the trade engine's daily-count and duplicate checks then see
    only that channel's history.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        tz: tzinfo = timezone.utc,
        source: str = DEFAULT_SOURCE,
    ) -> None:
        self._connection = connection
        self._tz = tz
        self._source = source

    @property
    def source(self) -> str:
        """The channel this repository is scoped to."""
        return self._source

    def add(self, signal: Signal, *, created_at: datetime | None = None) -> StoredSignal:
        """Persist ``signal`` and return the :class:`StoredSignal` row.

        Raises :class:`DuplicateSignalError` if the same signal was already
        stored **on the same trading day** — the existing row is left untouched.
        The same signal on a different day is accepted (a fresh trade).

        ``created_at`` (tz-aware) may be supplied to control the stored
        timestamp; it defaults to now (UTC). The trading day is ``created_at``
        as seen in the repository's ``tz``. Injecting ``created_at`` keeps time
        deterministic in tests and lets callers backfill rows.
        """
        message_hash = signal_hash(signal)
        created_at = created_at or datetime.now(timezone.utc)
        trade_date = created_at.astimezone(self._tz).date().isoformat()
        try:
            with self._connection:
                cursor = self._connection.execute(
                    """
                    INSERT INTO signals (
                        message_hash, underlying, strike, option_type, action,
                        entry_price, stop_loss, target, target_open_ended,
                        raw_text, created_at, trade_date, source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message_hash,
                        signal.underlying,
                        signal.strike,
                        signal.option_type.value,
                        signal.action.value,
                        signal.entry_price,
                        signal.stop_loss,
                        signal.target,
                        int(signal.target_open_ended),
                        signal.raw_text,
                        created_at.isoformat(),
                        trade_date,
                        self._source,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            # UNIQUE(message_hash, trade_date) is the per-day duplicate guard.
            logger.info(
                "Rejected duplicate signal (hash=%s date=%s)", message_hash, trade_date
            )
            raise DuplicateSignalError(message_hash) from exc

        stored = StoredSignal(
            id=int(cursor.lastrowid),
            message_hash=message_hash,
            created_at=created_at,
            signal=signal,
            source=self._source,
        )
        logger.info(
            "Stored signal id=%s (source=%s date=%s): %s",
            stored.id,
            self._source,
            trade_date,
            signal,
        )
        return stored

    def exists(self, signal: Signal, *, on_date: date | None = None) -> bool:
        """Return whether the same signal is already stored on ``on_date``.

        ``on_date`` defaults to today in the repository's ``tz``. Pass an
        explicit date (e.g. the trade engine's clock date) to keep the check
        deterministic and aligned with the caller's notion of "today".
        """
        target_date = (on_date or datetime.now(self._tz).date()).isoformat()
        row = self._connection.execute(
            "SELECT 1 FROM signals "
            "WHERE message_hash = ? AND trade_date = ? AND source = ? LIMIT 1",
            (signal_hash(signal), target_date, self._source),
        ).fetchone()
        return row is not None

    def get(self, signal_id: int) -> StoredSignal | None:
        """Fetch a stored signal by primary key, or ``None`` if absent."""
        row = self._connection.execute(
            "SELECT * FROM signals WHERE id = ?", (signal_id,)
        ).fetchone()
        return _row_to_stored(row) if row is not None else None

    def list_all(self) -> list[StoredSignal]:
        """Return every stored signal for this source, oldest first."""
        rows = self._connection.execute(
            "SELECT * FROM signals WHERE source = ? ORDER BY id ASC",
            (self._source,),
        ).fetchall()
        return [_row_to_stored(row) for row in rows]

    def count(self) -> int:
        """Return the number of stored signals for this source."""
        return int(
            self._connection.execute(
                "SELECT COUNT(*) FROM signals WHERE source = ?", (self._source,)
            ).fetchone()[0]
        )

    def count_since(self, moment: datetime) -> int:
        """Return how many signals for this source were stored at or after ``moment``.

        ``moment`` is normalised to UTC to match the stored ISO-8601 UTC
        ``created_at`` strings (which compare lexicographically). Used by the
        trade engine's daily-trade-limit rule.
        """
        boundary = moment.astimezone(timezone.utc).isoformat()
        row = self._connection.execute(
            "SELECT COUNT(*) FROM signals WHERE source = ? AND created_at >= ?",
            (self._source, boundary),
        ).fetchone()
        return int(row[0])


def _row_to_stored(row: sqlite3.Row) -> StoredSignal:
    """Reconstruct a :class:`StoredSignal` from a database row."""
    signal = Signal(
        underlying=row["underlying"],
        strike=int(row["strike"]),
        option_type=OptionType(row["option_type"]),
        action=Action(row["action"]),
        entry_price=float(row["entry_price"]),
        stop_loss=float(row["stop_loss"]),
        target=float(row["target"]),
        target_open_ended=bool(row["target_open_ended"]),
        raw_text=row["raw_text"],
    )
    return StoredSignal(
        id=int(row["id"]),
        message_hash=row["message_hash"],
        created_at=datetime.fromisoformat(row["created_at"]),
        signal=signal,
        source=row["source"],
    )
