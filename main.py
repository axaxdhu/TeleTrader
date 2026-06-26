"""Entry point for the Telegram Trader application.

Wires together config, the SQLite signal store, and the Telegram listener:
messages → parser → SQLite (Phase 3). No trading yet.
"""

from __future__ import annotations

import asyncio
import sys

from teletrader.config import Config, ConfigError
from teletrader.database import connect, initialize
from teletrader.logging_config import configure_logging, get_logger
from teletrader.repository import SignalRepository
from teletrader.telegram_listener import TelegramListener


def main() -> int:
    try:
        config = Config.from_env()
    except ConfigError as exc:
        # Logging isn't configured yet; report straight to stderr.
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    configure_logging(config.log_level)
    logger = get_logger("teletrader")

    connection = connect(config.database_path)
    initialize(connection)
    repository = SignalRepository(connection)

    listener = TelegramListener(config, repository)
    try:
        asyncio.run(listener.run())
    except KeyboardInterrupt:
        logger.info("Shutting down (interrupted by user).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
