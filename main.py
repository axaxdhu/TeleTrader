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
from teletrader.execution import (
    DRY_RUN_MODE,
    FYERS_SHADOW_MODE,
    ExecutionRepository,
    create_executor,
)
from teletrader.lot_size import BrokerLotSizeProvider, ConfigLotSizeProvider
from teletrader.logging_config import configure_logging, get_logger
from teletrader.notifier import create_notifier
from teletrader.pipeline import Channel2Pipeline, SignalPipeline
from teletrader.repository import SignalRepository
from teletrader.telegram_listener import ChannelSubscription, TelegramListener
from teletrader.trade_engine import TradeEngine
from teletrader.trade_manager import TradeManager
from teletrader.trade_repository import TradeRepository


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
    market_tz = ZoneInfo(config.market_timezone)
    signal_repository = SignalRepository(connection, tz=market_tz)
    execution_repository = ExecutionRepository(connection)
    trade_repository = TradeRepository(connection, tz=market_tz)

    # Decision + execution: the engine decides; the configured executor submits.
    # The broker is selected per channel (Config.broker_for), so channel 1 uses
    # its own broker (dry_run / kite / fyers). The trade manager applies management
    # commands (avoid / book profit / move SL) to active trades. The pipeline
    # glues the stages.
    channel_1_broker = config.broker_for("channel1")
    engine = TradeEngine(config, signal_repository)
    executor = create_executor(config, execution_repository, mode=channel_1_broker)
    trade_manager = TradeManager(trade_repository, executor)
    pipeline = SignalPipeline(signal_repository, engine, trade_manager)
    logger.info("Channel 1 broker: %s", channel_1_broker)

    # One subscription per enabled channel. Channel 1 is the full trading
    # pipeline; channel 2 stores into its own source-scoped repository so its
    # signals never mix with channel 1's history.
    subscriptions: list[ChannelSubscription] = []
    if config.channel_1_enabled:
        subscriptions.append(ChannelSubscription("channel1", config.channel, pipeline))
    if config.channel_2 is not None and config.channel_2_enabled:
        channel_2_broker = config.broker_for("channel2")
        # Channel 2 has no trade state machine yet, so it cannot place protective
        # stop-loss/target orders. Refusing a live broker here is deliberate:
        # an unprotected live entry is worse than no entry at all.
        if channel_2_broker not in (DRY_RUN_MODE, FYERS_SHADOW_MODE):
            print(
                f"CHANNEL_2_BROKER={channel_2_broker!r} would place live, "
                "UNPROTECTED orders: channel 2 has no trade manager yet (no "
                f"stop-loss or target is placed). Use {FYERS_SHADOW_MODE!r} until "
                "protection is wired up.",
                file=sys.stderr,
            )
            return 1
        channel2_repository = SignalRepository(
            connection, tz=market_tz, source="channel2"
        )
        channel_2_executor = create_executor(
            config, execution_repository, mode=channel_2_broker
        )
        # Channel 2 trades stock options, whose lot sizes are not in LOT_SIZES
        # (it only ever listed the index underlyings). Take them from the same
        # broker instrument master that resolves the tradingsymbol, falling back
        # to the configured sizes when the executor has no master (dry run).
        lot_size_source = getattr(channel_2_executor, "lot_size_source", None)
        lot_size_provider = (
            BrokerLotSizeProvider(
                lot_size_source,
                fallback=ConfigLotSizeProvider(config.lot_sizes),
                tz=market_tz,
            )
            if lot_size_source is not None
            else None
        )
        channel_2_engine = TradeEngine(
            config, channel2_repository, lot_size_provider=lot_size_provider
        )
        subscriptions.append(
            ChannelSubscription(
                "channel2",
                config.channel_2,
                Channel2Pipeline(
                    channel2_repository, channel_2_engine, channel_2_executor
                ),
            )
        )
        logger.info(
            "Channel 2 broker: %s (lot sizes: %s)",
            channel_2_broker,
            "broker master" if lot_size_provider else "config",
        )

    if not subscriptions:
        print(
            "No channels enabled: set CHANNEL_1_ENABLED / CHANNEL_2_ENABLED "
            "(and TELEGRAM_CHANNEL_2 for the second channel).",
            file=sys.stderr,
        )
        return 1

    # Optional push alerts (recognised signal + outcome) via a Telegram bot.
    notifier = create_notifier(config)
    logger.info("Notifications: %s", "on" if config.notify_enabled else "off")

    listener = TelegramListener(config, subscriptions, notifier=notifier)
    try:
        asyncio.run(listener.run())
    except KeyboardInterrupt:
        logger.info("Shutting down (interrupted by user).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
