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
