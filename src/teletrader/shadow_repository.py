"""Persistence for shadow runs — the record a day's P&L is computed from.

A shadow run is an order that was built completely and deliberately not sent.
:mod:`teletrader.execution.repository` already records *that* an attempt happened
and how it was judged, but a verdict and a prose remark cannot be priced. To
answer "would this day have been profitable?" the resolved contract, the exact
quantity and the three prices have to survive as columns — which is what this
module stores.

Rows are written when the signal is shadowed and **scored later**, at the end of
the trading day, once market data can say whether the target or the stop came
first. An unscored row keeps ``outcome``/``pnl`` NULL, so "not yet judged" is
never mistaken for "broke even".
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone, tzinfo
from enum import Enum

from .execution.models import ExecutionResult, ExecutionStatus
from .logging_config import get_logger

__all__ = ["Outcome", "ShadowRepository", "ShadowRun", "StoredShadowRun"]

logger = get_logger(__name__)


class Outcome(str, Enum):
    """How a shadowed trade would have ended."""

    TARGET = "target"        # the target was reached first
    STOPPED = "stopped"      # the stop-loss was hit first
    OPEN = "open"            # neither; marked to the closing price
    UNKNOWN = "unknown"      # market data could not settle it


@dataclass(frozen=True, slots=True)
class ShadowRun:
    """A shadowed order as built, before anything is known about its outcome."""

    source: str
    tradingsymbol: str
    exchange: str
    expiry: date
    quantity: int
    lot_size: int
    accepted: bool
    protected: bool
    signal_id: int | None = None
    underlying: str | None = None
    entry_price: float | None = None
    stop_loss: float | None = None
    target: float | None = None
    remarks: str | None = None


@dataclass(frozen=True, slots=True)
class StoredShadowRun:
    """A persisted shadow run, with its outcome once the day has been scored."""

    id: int
    trade_date: str
    created_at: datetime
    run: ShadowRun
    outcome: Outcome | None = None
    exit_price: float | None = None
    pnl: float | None = None

    @property
    def scored(self) -> bool:
        return self.outcome is not None


class ShadowRepository:
    """Stores and retrieves shadow runs in SQLite (injected connection).

    Scoped to a trading day in the market's timezone, like the signal
    repository — a day's report must not straddle a UTC midnight that falls in
    the middle of an Indian trading session.
    """

    def __init__(
        self, connection: sqlite3.Connection, *, tz: tzinfo = timezone.utc
    ) -> None:
        self._connection = connection
        self._tz = tz

    def add(self, run: ShadowRun, *, created_at: datetime | None = None) -> int:
        """Record a shadow run and return its row id."""
        moment = (created_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        trade_date = moment.astimezone(self._tz).date().isoformat()
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO shadow_runs (
                    signal_id, source, trade_date, created_at, tradingsymbol,
                    exchange, expiry, underlying, quantity, lot_size,
                    entry_price, stop_loss, target, accepted, protected, remarks
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.signal_id,
                    run.source,
                    trade_date,
                    moment.isoformat(),
                    run.tradingsymbol,
                    run.exchange,
                    run.expiry.isoformat(),
                    run.underlying,
                    run.quantity,
                    run.lot_size,
                    run.entry_price,
                    run.stop_loss,
                    run.target,
                    int(run.accepted),
                    int(run.protected),
                    run.remarks,
                ),
            )
        return int(cursor.lastrowid or 0)

    def for_day(self, day: date, *, source: str | None = None) -> list[StoredShadowRun]:
        """Return the runs recorded on ``day`` (optionally for one channel)."""
        sql = "SELECT * FROM shadow_runs WHERE trade_date = ?"
        params: list[object] = [day.isoformat()]
        if source is not None:
            sql += " AND source = ?"
            params.append(source)
        sql += " ORDER BY id"
        return [_row_to_run(row) for row in self._connection.execute(sql, params)]

    def score(
        self,
        run_id: int,
        *,
        outcome: Outcome,
        exit_price: float | None,
        pnl: float | None,
        scored_at: datetime | None = None,
    ) -> None:
        """Record how a run would have ended."""
        moment = (scored_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self._connection:
            self._connection.execute(
                """
                UPDATE shadow_runs
                   SET outcome = ?, exit_price = ?, pnl = ?, scored_at = ?
                 WHERE id = ?
                """,
                (outcome.value, exit_price, pnl, moment.isoformat(), run_id),
            )

    def today(self, *, now: datetime | None = None, source: str | None = None) -> list[StoredShadowRun]:
        """Return the current trading day's runs."""
        moment = (now or datetime.now(timezone.utc)).astimezone(self._tz)
        return self.for_day(moment.date(), source=source)


def run_from_execution(
    result: ExecutionResult, *, source: str, signal_id: int | None
) -> ShadowRun | None:
    """Build a :class:`ShadowRun` from a shadow execution, or ``None``.

    Returns ``None`` for any result that carries no shadow report — a dry run or
    a live order has nothing to price here, and an entry rejected before symbol
    resolution has no contract to price it against.
    """
    report = result.shadow
    if report is None:
        return None
    order = result.order
    return ShadowRun(
        source=source,
        tradingsymbol=report.tradingsymbol,
        exchange=report.exchange,
        expiry=report.expiry,
        quantity=report.quantity,
        lot_size=report.lot_size,
        accepted=result.status is ExecutionStatus.SUCCESS,
        protected=report.fully_protected,
        signal_id=signal_id,
        underlying=order.underlying,
        entry_price=order.entry_price,
        stop_loss=order.stop_loss,
        target=order.target,
        remarks=result.remarks,
    )


def _row_to_run(row: sqlite3.Row) -> StoredShadowRun:
    """Map a database row back to a :class:`StoredShadowRun`."""
    return StoredShadowRun(
        id=int(row["id"]),
        trade_date=str(row["trade_date"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        run=ShadowRun(
            source=str(row["source"]),
            tradingsymbol=str(row["tradingsymbol"]),
            exchange=str(row["exchange"]),
            expiry=date.fromisoformat(str(row["expiry"])),
            quantity=int(row["quantity"]),
            lot_size=int(row["lot_size"]),
            accepted=bool(row["accepted"]),
            protected=bool(row["protected"]),
            signal_id=row["signal_id"],
            underlying=row["underlying"],
            entry_price=row["entry_price"],
            stop_loss=row["stop_loss"],
            target=row["target"],
            remarks=row["remarks"],
        ),
        outcome=Outcome(row["outcome"]) if row["outcome"] else None,
        exit_price=row["exit_price"],
        pnl=row["pnl"],
    )
