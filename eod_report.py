"""Send the end-of-day shadow P&L summary.

Run once after the close (a systemd timer does this on the server). It scores
every shadow run recorded today against the contract's own intraday candles and
pushes one Telegram summary: what would have hit its target, what would have been
stopped, and what the day would have been worth.

    uv run python eod_report.py              # today, channel 2
    uv run python eod_report.py --date 2026-09-28
    uv run python eod_report.py --print      # print instead of sending

Needs a valid daily FYERS access token: the outcomes come from the broker's
history endpoint, so the prices are the ones the contract actually traded at.
Without a token the trades are reported **unscored** rather than guessed at — the
summary still arrives and says plainly what it could not settle.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

from teletrader.config import Config, ConfigError
from teletrader.database import connect, initialize
from teletrader.eod import FyersCandleSource, build_report
from teletrader.execution.fyers import _build_client
from teletrader.logging_config import configure_logging, get_logger
from teletrader.notifier import create_notifier
from teletrader.shadow_repository import ShadowRepository


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        help="Trading day to report on (YYYY-MM-DD). Default: today in the market timezone.",
    )
    parser.add_argument(
        "--source", default="channel2", help="Channel to report on (default: channel2)."
    )
    parser.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="Print the summary instead of sending it to Telegram.",
    )
    args = parser.parse_args()

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    configure_logging(config.log_level)
    logger = get_logger("teletrader.eod")

    market_tz = ZoneInfo(config.market_timezone)
    day = (
        date.fromisoformat(args.date)
        if args.date
        else datetime.now(market_tz).date()
    )

    connection = connect(config.database_path)
    initialize(connection)
    repository = ShadowRepository(connection, tz=market_tz)

    # The candle source needs an authenticated client. If the daily token is
    # missing the report is still produced — every trade simply comes back
    # unscored, which is the honest outcome and visible to the reader.
    try:
        client = _build_client(config.fyers_app_id, config.fyers_access_token)
        candles = FyersCandleSource(client, tz=market_tz)
    except Exception as exc:  # noqa: BLE001 — a missing token must not lose the report
        logger.warning("No usable FYERS client (%s); trades will be unscored.", exc)
        candles = _NoCandles()

    summary = build_report(repository, candles, day=day, source=args.source)

    if args.print_only:
        print(summary)
        return 0

    create_notifier(config).notify(summary)
    logger.info("End-of-day summary sent for %s (%s).", day.isoformat(), args.source)
    return 0


class _NoCandles:
    """Used when there is no authenticated client: settles nothing, hides nothing."""

    def candles(self, tradingsymbol: str, day: date) -> list:  # noqa: ARG002
        raise RuntimeError("no FYERS access token — cannot fetch market data")


if __name__ == "__main__":
    raise SystemExit(main())
