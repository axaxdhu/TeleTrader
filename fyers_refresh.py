"""Mint today's FYERS access token from the refresh token — no browser needed.

A systemd timer runs this each morning before the market opens. It replaces the
daily login, which was the system's weakest point: a three-hour window that,
when missed, silently and permanently destroyed that day's scoring (it was missed
eight days running).

    uv run python fyers_refresh.py            # refresh and write .env
    uv run python fyers_refresh.py --print    # say what would happen, write nothing

Needs ``FYERS_REFRESH_TOKEN`` (written by ``fyers_login.py``) and ``FYERS_PIN``.
The refresh token lasts about **fifteen days**, so a browser login is still
needed roughly fortnightly — this warns on Telegram when that is due, and when a
refresh fails for any reason.

Exits 0 on success and 1 on failure, so the unit's status records the answer.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fyers_login import write_env_values
from teletrader.config import Config, ConfigError
from teletrader.fyers_auth import refresh_access_token
from teletrader.logging_config import configure_logging, get_logger
from teletrader.notifier import create_notifier
from teletrader.token_check import token_expiry

#: Warn this far ahead of the refresh token lapsing, so the manual login can be
#: done calmly rather than discovered on a morning when it has already failed.
RELOGIN_WARNING = timedelta(days=3)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="Report the outcome instead of writing .env or sending alerts.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(__file__).resolve().parent / ".env",
        help="Path to the .env file to update.",
    )
    args = parser.parse_args()

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    configure_logging(config.log_level)
    logger = get_logger("teletrader.fyers_refresh")
    market_tz = ZoneInfo(config.market_timezone)

    result = refresh_access_token(
        app_id=config.fyers_app_id,
        secret_id=config.fyers_secret_id,
        refresh_token=config.fyers_refresh_token,
        pin=config.fyers_pin,
    )

    if not result.ok:
        logger.warning("Daily token refresh failed: %s", result.detail)
        alert = _failure_alert(
            result.detail,
            needs_relogin=result.needs_relogin,
            not_configured=result.not_configured,
        )
        if args.print_only:
            print(alert)
        else:
            create_notifier(config).notify(alert)
        return 1

    assert result.access_token is not None
    expires_at = token_expiry(result.access_token, tz=market_tz)
    until = f" (valid until {expires_at:%a %d %b %H:%M})" if expires_at else ""

    if args.print_only:
        print(f"✅ Would refresh the access token{until}.")
    else:
        write_env_values(args.env_file, {"FYERS_ACCESS_TOKEN": result.access_token})
        logger.info("Daily token refreshed%s.", until)

    # The refresh token itself lapses in about a fortnight. Say so *before* it
    # does: discovering it on the morning it fails is how a week goes missing.
    due = token_expiry(config.fyers_refresh_token, tz=market_tz)
    if due is not None:
        left = due - datetime.now(timezone.utc).astimezone(market_tz)
        if left <= RELOGIN_WARNING:
            message = (
                "🔑 FYERS re-login due\n"
                f"The refresh token expires {due:%a %d %b %H:%M} "
                f"({max(left.days, 0)} day(s) left). The daily refresh stops working "
                "then. Run once, any time before:\n"
                "  uv run python fyers_login.py --manual"
            )
            logger.warning("Refresh token expires %s", due.isoformat())
            if args.print_only:
                print(message)
            else:
                create_notifier(config).notify(message)

    return 0


def _failure_alert(
    detail: str, *, needs_relogin: bool, not_configured: bool = False
) -> str:
    """Render the alert for a failed refresh."""
    lines = ["⚠️ FYERS daily token refresh FAILED", detail, ""]
    if not_configured:
        lines.append(
            "Nothing is broken — the unattended refresh is simply not set up. "
            "Run the browser login once to obtain a refresh token, and set "
            "FYERS_PIN in .env:"
        )
        lines.append("  uv run python fyers_login.py --manual")
    elif needs_relogin:
        lines.append(
            "This needs a browser login — retrying will not help. The refresh "
            "token lasts about 15 days and has probably lapsed:"
        )
        lines.append("  uv run python fyers_login.py --manual")
    else:
        lines.append(
            "This looks transient. It will be retried tomorrow, but check if "
            "today's P&L matters."
        )
    lines.append("")
    lines.append(
        "Until it succeeds there is no funds check and no end-of-day P&L. "
        "Signal alerts are unaffected."
    )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
