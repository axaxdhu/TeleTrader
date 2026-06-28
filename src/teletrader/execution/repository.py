"""Persistence for execution attempts (the ``executions`` table).

Records every execution *attempt* — what would have been (dry run) or was (live)
submitted, and the verdict — so the pipeline can be audited end-to-end. This is
**not** a record of market fills or positions; it captures the order as sent and
its status only.

Follows the project's ``database`` (where) vs ``repository`` (what) split: the
schema lives in :mod:`teletrader.database`; this module owns the row mapping and
is the only place that issues SQL against ``executions``. The connection is
injected (DI).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from ..logging_config import get_logger
from .models import ExecutionResult, ExecutionStatus, OrderType, TransactionType

__all__ = ["ExecutionRepository", "StoredExecution"]

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class StoredExecution:
    """An execution attempt as persisted: the order as sent plus the verdict."""

    id: int
    signal_id: int | None
    timestamp: datetime
    symbol: str
    action: TransactionType
    quantity: int
    order_type: OrderType
    entry_price: float | None
    stop_loss: float | None
    target: float | None
    status: ExecutionStatus
    remarks: str | None
    broker_order_id: str | None = None


class ExecutionRepository:
    """Stores and retrieves execution attempts in SQLite (injected connection)."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def add(self, result: ExecutionResult) -> StoredExecution:
        """Persist the attempt described by ``result`` and return the stored row."""
        order = result.order
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO executions (
                    signal_id, timestamp, symbol, action, quantity, order_type,
                    entry_price, stop_loss, target, status, remarks, broker_order_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order.signal_id,
                    result.timestamp.isoformat(),
                    order.symbol,
                    order.transaction_type.value,
                    order.quantity,
                    order.order_type.value,
                    order.entry_price,
                    order.stop_loss,
                    order.target,
                    result.status.value,
                    result.remarks,
                    result.broker_order_id,
                ),
            )
        stored = self._row_to_stored(
            self._connection.execute(
                "SELECT * FROM executions WHERE id = ?", (int(cursor.lastrowid),)
            ).fetchone()
        )
        logger.debug(
            "Recorded execution id=%s signal_id=%s status=%s",
            stored.id,
            stored.signal_id,
            stored.status.value,
        )
        return stored

    def get(self, execution_id: int) -> StoredExecution | None:
        row = self._connection.execute(
            "SELECT * FROM executions WHERE id = ?", (execution_id,)
        ).fetchone()
        return self._row_to_stored(row) if row is not None else None

    def list_all(self) -> list[StoredExecution]:
        rows = self._connection.execute(
            "SELECT * FROM executions ORDER BY id ASC"
        ).fetchall()
        return [self._row_to_stored(row) for row in rows]

    def list_for_signal(self, signal_id: int) -> list[StoredExecution]:
        rows = self._connection.execute(
            "SELECT * FROM executions WHERE signal_id = ? ORDER BY id ASC",
            (signal_id,),
        ).fetchall()
        return [self._row_to_stored(row) for row in rows]

    def count(self) -> int:
        return int(
            self._connection.execute("SELECT COUNT(*) FROM executions").fetchone()[0]
        )

    @staticmethod
    def _row_to_stored(row: sqlite3.Row) -> StoredExecution:
        return StoredExecution(
            id=int(row["id"]),
            signal_id=row["signal_id"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            symbol=row["symbol"],
            action=TransactionType(row["action"]),
            quantity=int(row["quantity"]),
            order_type=OrderType(row["order_type"]),
            entry_price=row["entry_price"],
            stop_loss=row["stop_loss"],
            target=row["target"],
            status=ExecutionStatus(row["status"]),
            remarks=row["remarks"],
            broker_order_id=row["broker_order_id"],
        )
