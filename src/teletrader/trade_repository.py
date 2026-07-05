"""Persistence layer for active trades.

Provides :class:`TradeRepository`, a thin repository over the ``trades`` table
that tracks the live position opened by an executed entry signal through its
lifecycle (:class:`TradeStatus`). This is the state the trade-management commands
(avoid / book profit / move stop-loss) act on: a command targets the **most
recently active** trade (see :meth:`TradeRepository.most_recent_active`).

Mirrors :mod:`teletrader.repository`: the connection is injected (DI), all SQL for
the ``trades`` table lives here, and rows map to/from a frozen :class:`StoredTrade`.
Broker handles (``tradingsymbol``, the order ids) are nullable — they are filled
in once a live executor places and fills the entry and its protective orders; a
dry run leaves them ``None``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from enum import Enum

from .logging_config import get_logger
from .parser import OptionType

__all__ = ["StoredTrade", "TradeRepository", "TradeStatus"]

logger = get_logger(__name__)


class TradeStatus(str, Enum):
    """Where a trade is in its lifecycle.

    ``PENDING_ENTRY`` — the entry order is placed but not yet filled (avoidable:
    it can still be cancelled). ``OPEN`` — the entry filled and the position is
    live (book-profit / move-SL apply). ``CLOSED`` — the position was exited
    (booked, stopped out, or target hit). ``CANCELLED`` — the entry was cancelled
    before it filled.
    """

    PENDING_ENTRY = "PENDING_ENTRY"
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


#: Statuses considered "active" — a management command targets one of these.
ACTIVE_STATUSES: tuple[TradeStatus, ...] = (TradeStatus.PENDING_ENTRY, TradeStatus.OPEN)


@dataclass(frozen=True, slots=True)
class StoredTrade:
    """An active trade as persisted: the position plus its broker handles."""

    id: int
    signal_id: int | None
    underlying: str
    strike: int
    option_type: OptionType
    quantity: int
    status: TradeStatus
    entry_price: float | None
    stop_loss: float | None
    target: float | None
    tradingsymbol: str | None
    entry_order_id: str | None
    sl_order_id: str | None
    target_order_id: str | None
    created_at: datetime
    updated_at: datetime

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES


class TradeRepository:
    """Stores and updates active trades in SQLite.

    The connection's lifecycle is not owned here — it is injected (DI), so tests
    pass an in-memory connection and the application shares one. ``tz`` is unused
    for storage (timestamps are stored in UTC) but kept for symmetry with the
    other repositories and future day-scoped queries; it defaults to UTC.
    """

    def __init__(
        self, connection: sqlite3.Connection, *, tz: tzinfo = timezone.utc
    ) -> None:
        self._connection = connection
        self._tz = tz

    def open_trade(
        self,
        *,
        signal_id: int | None,
        underlying: str,
        strike: int,
        option_type: OptionType,
        quantity: int,
        status: TradeStatus = TradeStatus.PENDING_ENTRY,
        entry_price: float | None = None,
        stop_loss: float | None = None,
        target: float | None = None,
        tradingsymbol: str | None = None,
        entry_order_id: str | None = None,
        sl_order_id: str | None = None,
        target_order_id: str | None = None,
        created_at: datetime | None = None,
    ) -> StoredTrade:
        """Insert a new trade row and return it.

        ``created_at`` (tz-aware) may be supplied to control the stored timestamps
        (kept deterministic in tests); it defaults to now (UTC) and also seeds
        ``updated_at``.
        """
        moment = created_at or datetime.now(timezone.utc)
        stamp = moment.isoformat()
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO trades (
                    signal_id, underlying, strike, option_type, tradingsymbol,
                    quantity, status, entry_price, stop_loss, target,
                    entry_order_id, sl_order_id, target_order_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal_id,
                    underlying,
                    strike,
                    option_type.value,
                    tradingsymbol,
                    quantity,
                    status.value,
                    entry_price,
                    stop_loss,
                    target,
                    entry_order_id,
                    sl_order_id,
                    target_order_id,
                    stamp,
                    stamp,
                ),
            )
        trade_id = int(cursor.lastrowid)
        logger.info(
            "Opened trade id=%s (%s %s %s qty=%s status=%s)",
            trade_id, underlying, strike, option_type.value, quantity, status.value,
        )
        stored = self.get(trade_id)
        assert stored is not None  # just inserted
        return stored

    def most_recent_active(self) -> StoredTrade | None:
        """Return the latest still-active trade (``PENDING_ENTRY`` or ``OPEN``).

        This is the trade a management command targets — the channel posts a bare
        "Avoid" / "BOOK PROFIT" / "MODIFY SL TO COST" and means the position it
        most recently told us to open. ``None`` if there is no active trade.
        """
        row = self._connection.execute(
            """
            SELECT * FROM trades
             WHERE status IN (?, ?)
             ORDER BY id DESC
             LIMIT 1
            """,
            (TradeStatus.PENDING_ENTRY.value, TradeStatus.OPEN.value),
        ).fetchone()
        return _row_to_trade(row) if row is not None else None

    def get(self, trade_id: int) -> StoredTrade | None:
        """Fetch a trade by primary key, or ``None`` if absent."""
        row = self._connection.execute(
            "SELECT * FROM trades WHERE id = ?", (trade_id,)
        ).fetchone()
        return _row_to_trade(row) if row is not None else None

    def list_active(self) -> list[StoredTrade]:
        """Return every active trade, newest first."""
        rows = self._connection.execute(
            """
            SELECT * FROM trades
             WHERE status IN (?, ?)
             ORDER BY id DESC
            """,
            (TradeStatus.PENDING_ENTRY.value, TradeStatus.OPEN.value),
        ).fetchall()
        return [_row_to_trade(row) for row in rows]

    def update_status(
        self, trade_id: int, status: TradeStatus, *, when: datetime | None = None
    ) -> None:
        """Move ``trade_id`` to ``status`` and bump ``updated_at``."""
        self._touch(trade_id, "status = ?", (status.value,), when)
        logger.info("Trade id=%s -> %s", trade_id, status.value)

    def update_stop_loss(
        self, trade_id: int, stop_loss: float, *, when: datetime | None = None
    ) -> None:
        """Record a new stop-loss level for ``trade_id`` (e.g. moved to cost)."""
        self._touch(trade_id, "stop_loss = ?", (stop_loss,), when)
        logger.info("Trade id=%s stop_loss -> %s", trade_id, stop_loss)

    def update_target(
        self, trade_id: int, target: float, *, when: datetime | None = None
    ) -> None:
        """Record a new target level for ``trade_id``."""
        self._touch(trade_id, "target = ?", (target,), when)
        logger.info("Trade id=%s target -> %s", trade_id, target)

    def set_broker_handles(
        self,
        trade_id: int,
        *,
        tradingsymbol: str | None = None,
        entry_order_id: str | None = None,
        sl_order_id: str | None = None,
        target_order_id: str | None = None,
        entry_price: float | None = None,
        when: datetime | None = None,
    ) -> None:
        """Fill in broker-side handles once they are known (live executor).

        Only the provided fields are written; the rest are left untouched, so this
        can be called incrementally (e.g. entry id at placement, fill price + SL id
        once the entry fills).
        """
        assignments: list[str] = []
        params: list[object] = []
        for column, value in (
            ("tradingsymbol", tradingsymbol),
            ("entry_order_id", entry_order_id),
            ("sl_order_id", sl_order_id),
            ("target_order_id", target_order_id),
            ("entry_price", entry_price),
        ):
            if value is not None:
                assignments.append(f"{column} = ?")
                params.append(value)
        if not assignments:
            return
        self._touch(trade_id, ", ".join(assignments), tuple(params), when)

    def _touch(
        self,
        trade_id: int,
        assignment: str,
        params: tuple[object, ...],
        when: datetime | None,
    ) -> None:
        """Apply a column update plus ``updated_at`` for ``trade_id``."""
        stamp = (when or datetime.now(timezone.utc)).isoformat()
        with self._connection:
            self._connection.execute(
                f"UPDATE trades SET {assignment}, updated_at = ? WHERE id = ?",
                (*params, stamp, trade_id),
            )


def _row_to_trade(row: sqlite3.Row) -> StoredTrade:
    """Reconstruct a :class:`StoredTrade` from a database row."""
    return StoredTrade(
        id=int(row["id"]),
        signal_id=int(row["signal_id"]) if row["signal_id"] is not None else None,
        underlying=row["underlying"],
        strike=int(row["strike"]),
        option_type=OptionType(row["option_type"]),
        quantity=int(row["quantity"]),
        status=TradeStatus(row["status"]),
        entry_price=_opt_float(row["entry_price"]),
        stop_loss=_opt_float(row["stop_loss"]),
        target=_opt_float(row["target"]),
        tradingsymbol=row["tradingsymbol"],
        entry_order_id=row["entry_order_id"],
        sl_order_id=row["sl_order_id"],
        target_order_id=row["target_order_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _opt_float(value: object) -> float | None:
    return float(value) if value is not None else None
