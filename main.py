"""Entry point for the Telegram Trader application.

Composition root: builds config, logging, the SQLite store, the trade engine, and
the order executor, wires them into the signal pipeline, and runs the Telegram
listener. The full path is *message → parse → store → evaluate → execute*; the
executor is selected by ``EXECUTION_MODE`` (``dry_run`` today).
"""

from __future__ import annotations

import asyncio
import sys
from zoneinfo import ZoneInfo

from teletrader.config import Config, ConfigError
from teletrader.database import connect, initialize
from teletrader.execution import ExecutionRepository, create_executor
from teletrader.logging_config import configure_logging, get_logger
from teletrader.pipeline import SignalPipeline
from teletrader.repository import SignalRepository
from teletrader.telegram_listener import TelegramListener
from teletrader.trade_engine import TradeEngine


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

    # Persistence: dedupe is per trading day, so the signal repository needs the
    # market timezone to know when "today" rolls over.
    signal_repository = SignalRepository(connection, tz=ZoneInfo(config.market_timezone))
    execution_repository = ExecutionRepository(connection)

    # Decision + execution: the engine decides; the configured executor submits
    # (a dry run unless EXECUTION_MODE=kite). The pipeline glues the stages.
    engine = TradeEngine(config, signal_repository)
    executor = create_executor(config, execution_repository)
    pipeline = SignalPipeline(signal_repository, engine, executor)
    logger.info("Execution mode: %s", config.execution_mode)

    listener = TelegramListener(config, pipeline)
    try:
        asyncio.run(listener.run())
    except KeyboardInterrupt:
        logger.info("Shutting down (interrupted by user).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
