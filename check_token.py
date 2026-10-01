"""Pre-market check that today's FYERS token is alive; alert if it is not.

A systemd timer runs this on the server shortly before the market opens. It asks
the broker whether the token still works and pushes a Telegram warning **only if
it does not** — a notification that arrives every morning is one nobody reads.

    uv run python check_token.py            # check and alert if broken
    uv run python check_token.py --print    # print the verdict, send nothing

Exits 0 when the token is healthy and 1 when it is not, so the timer's status
also records the answer.
"""

from __future__ import annotations

import argparse
import sys
from zoneinfo import ZoneInfo

from teletrader.config import Config, ConfigError
from teletrader.execution.fyers import _build_client
from teletrader.logging_config import configure_logging, get_logger
from teletrader.notifier import create_notifier
from teletrader.token_check import TokenStatus, format_token_alert, probe_token


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="Print the verdict instead of sending an alert.",
    )
    args = parser.parse_args()

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    configure_logging(config.log_level)
    logger = get_logger("teletrader.token_check")

    try:
        client = _build_client(config.fyers_app_id, config.fyers_access_token)
    except Exception as exc:  # noqa: BLE001 — a missing token is the thing we report
        status = TokenStatus(False, str(exc))
    else:
        # The token is checked against the market close, not just "does it work
        # now": FYERS expires tokens at a fixed 06:00 IST cutoff, so one made
        # overnight passes a naive check and still dies before the open.
        status = probe_token(
            client,
            token=config.fyers_access_token,
            market_close=config.market_close,
            tz=ZoneInfo(config.market_timezone),
        )

    if status.ok:
        # The date matters as much as the time: FYERS rolls the expiry to the
        # next 06:00 IST boundary, so "valid until 06:00" is reassuring when it
        # means tomorrow and alarming when it means this morning.
        until = (
            f" (valid until {status.expires_at:%a %d %b %H:%M})"
            if status.expires_at is not None
            else ""
        )
        logger.info("FYERS token check passed%s.", until)
        if args.print_only:
            print(f"✅ FYERS token is alive{until}.")
        return 0

    logger.warning("FYERS token check FAILED: %s", status.detail)
    alert = format_token_alert(status, market_open=config.market_open.strftime("%H:%M"))
    if args.print_only:
        print(alert)
    elif alert is not None:
        create_notifier(config).notify(alert)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
