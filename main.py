"""Entry point for the Telegram Trader application (Phase 1: listener only)."""

from __future__ import annotations

import asyncio
import sys

from teletrader.config import Config, ConfigError
from teletrader.logging_config import configure_logging, get_logger
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

    listener = TelegramListener(config)
    try:
        asyncio.run(listener.run())
    except KeyboardInterrupt:
        logger.info("Shutting down (interrupted by user).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
