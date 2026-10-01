"""SQLite database connection and schema management.

Owns the low-level concerns of *where* signals are stored: opening a tuned
SQLite connection and bringing its schema up to date via a small, ordered list
of migrations. The :mod:`teletrader.repository` module sits on top of this and
owns *what* gets stored.

Per ``CLAUDE.md``: SQLite is preferred over external databases, kept simple, no
ORM. Migrations are plain SQL keyed off SQLite's built-in ``user_version``
pragma so initialisation is idempotent and forward-only.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .logging_config import get_logger

__all__ = ["DEFAULT_DB_PATH", "SCHEMA_VERSION", "connect", "initialize"]

logger = get_logger(__name__)

#: Default on-disk location, relative to the working directory. Gitignored.
DEFAULT_DB_PATH = "teletrader.db"

# Ordered schema migrations. Each entry's index + 1 is the schema version it
# brings the database to; to evolve the schema, append a new statement and bump
# nothing else — :func:`initialize` only runs migrations newer than the
# database's current ``user_version``.
_MIGRATIONS: tuple[str, ...] = (
    # v1 — initial signals table.
    """
    CREATE TABLE signals (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        message_hash      TEXT    NOT NULL UNIQUE,
        underlying        TEXT    NOT NULL,
        strike            INTEGER NOT NULL,
        option_type       TEXT    NOT NULL,
        action            TEXT    NOT NULL,
        entry_price       REAL    NOT NULL,
        stop_loss         REAL    NOT NULL,
        target            REAL    NOT NULL,
        target_open_ended INTEGER NOT NULL,
        raw_text          TEXT    NOT NULL,
        created_at        TEXT    NOT NULL
    );
    """,
    # v2 — paper-trading orders (simulated broker, Phase 4).
    """
    CREATE TABLE paper_orders (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id         TEXT    NOT NULL UNIQUE,
        symbol           TEXT    NOT NULL,
        exchange         TEXT    NOT NULL,
        transaction_type TEXT    NOT NULL,
        quantity         INTEGER NOT NULL,
        order_type       TEXT    NOT NULL,
        product          TEXT    NOT NULL,
        price            REAL,
        trigger_price    REAL,
        status           TEXT    NOT NULL,
        filled_quantity  INTEGER NOT NULL DEFAULT 0,
        average_price    REAL,
        brokerage        REAL    NOT NULL DEFAULT 0,
        tag              TEXT,
        message          TEXT,
        created_at       TEXT    NOT NULL,
        updated_at       TEXT    NOT NULL
    );
    """,
    # v3 — paper-trading positions (one net row per instrument/product).
    """
    CREATE TABLE paper_positions (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol        TEXT    NOT NULL,
        exchange      TEXT    NOT NULL,
        product       TEXT    NOT NULL,
        quantity      INTEGER NOT NULL,
        average_price REAL    NOT NULL,
        last_price    REAL    NOT NULL,
        realized_pnl  REAL    NOT NULL DEFAULT 0,
        updated_at    TEXT    NOT NULL,
        UNIQUE(symbol, exchange, product)
    );
    """,
    # v4 — make signal dedupe per-day: add trade_date and key uniqueness on
    # (message_hash, trade_date) so the same signal is a duplicate only within
    # the same trading day, and is accepted again on a later day. SQLite cannot
    # drop the old table-level UNIQUE(message_hash), so the table is rebuilt;
    # existing rows backfill trade_date from the date part of created_at.
    """
    CREATE TABLE signals_v4 (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        message_hash      TEXT    NOT NULL,
        underlying        TEXT    NOT NULL,
        strike            INTEGER NOT NULL,
        option_type       TEXT    NOT NULL,
        action            TEXT    NOT NULL,
        entry_price       REAL    NOT NULL,
        stop_loss         REAL    NOT NULL,
        target            REAL    NOT NULL,
        target_open_ended INTEGER NOT NULL,
        raw_text          TEXT    NOT NULL,
        created_at        TEXT    NOT NULL,
        trade_date        TEXT    NOT NULL,
        UNIQUE(message_hash, trade_date)
    );
    INSERT INTO signals_v4 (
        id, message_hash, underlying, strike, option_type, action,
        entry_price, stop_loss, target, target_open_ended, raw_text,
        created_at, trade_date
    )
    SELECT id, message_hash, underlying, strike, option_type, action,
           entry_price, stop_loss, target, target_open_ended, raw_text,
           created_at, substr(created_at, 1, 10)
      FROM signals;
    DROP TABLE signals;
    ALTER TABLE signals_v4 RENAME TO signals;
    """,
    # v5 — replace the (removed) paper-broker tables with an execution-history
    # table. The project pivoted from a simulated broker to a thin order-execution
    # layer (DryRunExecutor / future KiteExecutor); paper_orders/paper_positions
    # are dropped, and `executions` records each execution *attempt* (the order as
    # it would be / was submitted, and its verdict) — not market fills.
    """
    DROP TABLE IF EXISTS paper_orders;
    DROP TABLE IF EXISTS paper_positions;
    CREATE TABLE executions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id   INTEGER REFERENCES signals(id),
        timestamp   TEXT    NOT NULL,
        symbol      TEXT    NOT NULL,
        action      TEXT    NOT NULL,
        quantity    INTEGER NOT NULL,
        order_type  TEXT    NOT NULL,
        entry_price REAL,
        stop_loss   REAL,
        target      REAL,
        status      TEXT    NOT NULL,
        remarks     TEXT
    );
    """,
    # v6 — record the broker's order id on each execution. The dry run leaves it
    # NULL (nothing is submitted); the live KiteExecutor stores the id Kite
    # returns, so a live attempt can be traced back to the broker.
    """
    ALTER TABLE executions ADD COLUMN broker_order_id TEXT;
    """,
    # v7 — active-trade state for trade-management commands (avoid / book profit /
    # move SL). A `trades` row is the live position opened by an executed entry
    # signal; management messages act on the most recent active one. The broker
    # order ids and resolved tradingsymbol are nullable — filled in once the live
    # executor places/fills the entry and its protective orders (Phase 2); a
    # dry run leaves them NULL.
    """
    CREATE TABLE trades (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id       INTEGER REFERENCES signals(id),
        underlying      TEXT    NOT NULL,
        strike          INTEGER NOT NULL,
        option_type     TEXT    NOT NULL,
        tradingsymbol   TEXT,
        quantity        INTEGER NOT NULL,
        status          TEXT    NOT NULL,
        entry_price     REAL,
        stop_loss       REAL,
        target          REAL,
        entry_order_id  TEXT,
        sl_order_id     TEXT,
        target_order_id TEXT,
        created_at      TEXT    NOT NULL,
        updated_at      TEXT    NOT NULL
    );
    CREATE INDEX idx_trades_status ON trades(status);
    """,
    # v8 — multi-channel: tag each signal with the channel it came from so per
    # channel storage, dedupe, and counting stay isolated (a second signal source
    # was added — see `channel2`). A plain ADD COLUMN is used (not a table rebuild)
    # because `executions`/`trades` now hold foreign keys into `signals`, and
    # dropping the parent table would violate them. Existing rows predate the
    # second channel, so they backfill to 'channel1'.
    #
    # Note: the day-scoped UNIQUE(message_hash, trade_date) is intentionally left
    # channel-agnostic. Each channel still dedupes within itself; the only edge
    # case is two channels posting an identically-hashing signal on the same day
    # (the second is treated as a duplicate). That is vanishingly rare given the
    # channels' different instruments/formats, and harmless for a parse-only
    # channel — a per-channel unique key would require the unsafe rebuild above.
    """
    ALTER TABLE signals ADD COLUMN source TEXT NOT NULL DEFAULT 'channel1';
    """,
    # v9 — shadow runs, the record an end-of-day P&L is computed from. The
    # `executions` table stores a verdict and a prose remark, which is enough to
    # audit an attempt but not to price one: answering "would this day have been
    # profitable?" needs the resolved contract, the exact quantity, and the three
    # prices, as columns. They live in their own table rather than as more
    # nullable columns on `executions` because only shadow runs have them, and
    # because a day's report is a simple scan of one trading date.
    #
    # `outcome`/`exit_price`/`pnl` are filled in later by the end-of-day job once
    # market data says whether the target or the stop came first; they stay NULL
    # until then, so an unscored row is distinguishable from a flat one.
    """
    CREATE TABLE shadow_runs (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id      INTEGER REFERENCES signals(id),
        source         TEXT    NOT NULL,
        trade_date     TEXT    NOT NULL,
        created_at     TEXT    NOT NULL,
        tradingsymbol  TEXT    NOT NULL,
        exchange       TEXT    NOT NULL,
        expiry         TEXT    NOT NULL,
        underlying     TEXT,
        quantity       INTEGER NOT NULL,
        lot_size       INTEGER NOT NULL,
        entry_price    REAL,
        stop_loss      REAL,
        target         REAL,
        accepted       INTEGER NOT NULL,
        protected      INTEGER NOT NULL,
        remarks        TEXT,
        outcome        TEXT,
        exit_price     REAL,
        pnl            REAL,
        scored_at      TEXT
    );
    CREATE INDEX idx_shadow_runs_date ON shadow_runs(trade_date, source);
    """,
    # v10 — tell "could not afford it" apart from the other reasons an order
    # would have been refused. `accepted` lumps them together, and scoring used
    # to skip every unaccepted run — so an empty trading account made the whole
    # P&L report blank, which answers the wrong question. Whether the *signal*
    # was any good is independent of whether that day's balance could fund it.
    # NULL where it was never determined (no token, or a sell).
    """
    ALTER TABLE shadow_runs ADD COLUMN funds_ok INTEGER;
    """,
)

#: The schema version this build expects. Equals the number of migrations.
SCHEMA_VERSION = len(_MIGRATIONS)


def connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a SQLite connection with project-standard pragmas.

    Pass ``":memory:"`` for an ephemeral database (used by tests). Rows are
    returned as :class:`sqlite3.Row` so callers can index by column name.
    """
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    # Enforce UNIQUE/NOT NULL and enable WAL for safer concurrent reads while the
    # listener writes. WAL is a no-op (silently ignored) for in-memory DBs.
    connection.execute("PRAGMA foreign_keys = ON;")
    connection.execute("PRAGMA journal_mode = WAL;")
    return connection


def initialize(connection: sqlite3.Connection) -> int:
    """Bring ``connection``'s schema up to :data:`SCHEMA_VERSION`.

    Idempotent and forward-only: applies just the migrations newer than the
    database's current ``user_version``, then records the new version. Returns
    the resulting schema version. Safe to call on every startup.
    """
    current = connection.execute("PRAGMA user_version;").fetchone()[0]
    if current >= SCHEMA_VERSION:
        logger.debug("Schema already at version %s; no migrations to apply.", current)
        return current

    with connection:  # single transaction: all-or-nothing
        for version in range(current, SCHEMA_VERSION):
            logger.info("Applying schema migration to version %s", version + 1)
            connection.executescript(_MIGRATIONS[version])
        # PRAGMA can't be parameterised; SCHEMA_VERSION is a trusted int.
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION};")

    return SCHEMA_VERSION
