"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from .database import DEFAULT_DB_PATH

#: Strings accepted as boolean true/false for env-var flags (case-insensitive).
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class Config:
    """Immutable application configuration.

    Values are sourced exclusively from environment variables so that no
    secrets live in the codebase. See ``.env.example`` for the full list.
    """

    api_id: int
    api_hash: str
    phone: str
    session_name: str
    channel: int | str
    log_level: str
    database_path: str
    # --- Trade engine (Phase 4 business logic) -------------------------------
    auto_trading: bool
    allow_duplicates: bool
    max_trades_per_day: int
    trade_lots: int
    lot_sizes: Mapping[str, int]
    market_open: time
    market_close: time
    market_timezone: str
    # --- Order execution (Phase 4) -------------------------------------------
    execution_mode: str
    # --- Live broker: Zerodha Kite (Phase 5) ---------------------------------
    # Only required when execution_mode == "kite". Authentication is manual: a
    # valid access token is assumed to already exist (this app never logs in nor
    # generates tokens). Secrets, never logged.
    kite_api_key: str | None
    kite_api_secret: str | None
    kite_access_token: str | None

    @classmethod
    def from_env(cls) -> "Config":
        """Build a :class:`Config` from the process environment.

        ``.env`` is loaded first (if present) so local development picks up
        values without exporting them manually.
        """
        load_dotenv()

        api_id_raw = _require("TELEGRAM_API_ID")
        try:
            api_id = int(api_id_raw)
        except ValueError as exc:
            raise ConfigError(
                "TELEGRAM_API_ID must be an integer"
            ) from exc

        execution_mode = _parse_execution_mode("EXECUTION_MODE", default="dry_run")

        return cls(
            api_id=api_id,
            api_hash=_require("TELEGRAM_API_HASH"),
            phone=_require("TELEGRAM_PHONE"),
            session_name=os.getenv("TELEGRAM_SESSION_NAME", "teletrader"),
            channel=_parse_channel(_require("TELEGRAM_CHANNEL")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            database_path=os.getenv("DATABASE_PATH", DEFAULT_DB_PATH),
            auto_trading=_parse_bool("AUTO_TRADING", default=False),
            allow_duplicates=_parse_bool("ALLOW_DUPLICATES", default=False),
            max_trades_per_day=_parse_int("MAX_TRADES_PER_DAY", default=10),
            trade_lots=_parse_int("TRADE_LOTS", default=1),
            lot_sizes=_parse_lot_sizes("LOT_SIZES"),
            market_open=_parse_time("MARKET_OPEN_TIME", default="09:15"),
            market_close=_parse_time("MARKET_CLOSE_TIME", default="15:30"),
            market_timezone=_parse_timezone("MARKET_TIMEZONE", default="Asia/Kolkata"),
            execution_mode=execution_mode,
            **_parse_kite_credentials(execution_mode),
        )


def _parse_channel(value: str) -> int | str:
    """Coerce a channel identifier to ``int`` when it is purely numeric.

    Telethon resolves numeric chat ids reliably only as integers; a numeric
    *string* is otherwise treated as a username. A leading ``-`` (e.g.
    ``-1001524695283`` for channels/supergroups) is preserved. Non-numeric
    values such as ``@safetrader90`` are returned unchanged.
    """
    candidate = value.strip()
    if candidate.lstrip("-").isdigit():
        return int(candidate)
    return candidate


def _require(name: str) -> str:
    """Return a required environment variable or raise :class:`ConfigError`."""
    value = os.getenv(name)
    if not value:
        raise ConfigError(f"Required environment variable {name!r} is not set")
    return value


def _parse_bool(name: str, *, default: bool) -> bool:
    """Parse a boolean env-var flag, falling back to ``default`` when unset."""
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ConfigError(f"{name} must be a boolean (true/false), got {raw!r}")


def _parse_int(name: str, *, default: int) -> int:
    """Parse an integer env var, falling back to ``default`` when unset."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


#: Order-execution modes. ``dry_run`` validates + logs without sending; ``kite``
#: (live) arrives in a later phase. Switching modes selects the executor.
_EXECUTION_MODES = frozenset({"dry_run", "kite"})


def _parse_execution_mode(name: str, *, default: str) -> str:
    """Parse and validate ``EXECUTION_MODE`` against the known modes."""
    value = os.getenv(name, default).strip().lower()
    if value not in _EXECUTION_MODES:
        allowed = ", ".join(sorted(_EXECUTION_MODES))
        raise ConfigError(f"{name} must be one of {{{allowed}}}, got {value!r}")
    return value


def _parse_kite_credentials(execution_mode: str) -> dict[str, str | None]:
    """Read the Kite credentials, requiring them only when running live.

    For ``EXECUTION_MODE=kite`` all three (``KITE_API_KEY``, ``KITE_API_SECRET``,
    ``KITE_ACCESS_TOKEN``) must be set — missing ones fail fast at startup. For
    ``dry_run`` they are optional (and usually absent). Authentication is manual:
    the access token is assumed already valid; this app never logs in.
    """
    creds = {
        "kite_api_key": os.getenv("KITE_API_KEY") or None,
        "kite_api_secret": os.getenv("KITE_API_SECRET") or None,
        "kite_access_token": os.getenv("KITE_ACCESS_TOKEN") or None,
    }
    if execution_mode == "kite":
        missing = [
            env
            for env, field in (
                ("KITE_API_KEY", "kite_api_key"),
                ("KITE_API_SECRET", "kite_api_secret"),
                ("KITE_ACCESS_TOKEN", "kite_access_token"),
            )
            if creds[field] is None
        ]
        if missing:
            raise ConfigError(
                "EXECUTION_MODE=kite requires "
                + ", ".join(missing)
                + " to be set"
            )
    return creds


def _parse_lot_sizes(name: str) -> Mapping[str, int]:
    """Parse ``SYM:size,SYM:size`` (e.g. ``NIFTY:65,BANKNIFTY:30``) into a map.

    Empty/unset yields an empty map; the trade engine then rejects any signal
    whose underlying has no configured lot size (fail-closed) rather than
    guessing a size. Lot sizes are exchange-defined and revised periodically —
    set them to the current values. See :mod:`teletrader.lot_size` for the
    future broker-backed source.
    """
    raw = os.getenv(name, "")
    sizes: dict[str, int] = {}
    for part in raw.split(","):
        entry = part.strip()
        if not entry:
            continue
        symbol, sep, size = entry.partition(":")
        if not sep or not symbol.strip():
            raise ConfigError(f"{name} must be 'SYM:size,SYM:size', got {raw!r}")
        try:
            sizes[symbol.strip().upper()] = int(size)
        except ValueError as exc:
            raise ConfigError(f"{name} must be 'SYM:size,SYM:size', got {raw!r}") from exc
    return sizes


def _parse_time(name: str, *, default: str) -> time:
    """Parse an ``HH:MM`` env var into a :class:`datetime.time`."""
    raw = os.getenv(name, default)
    try:
        return datetime.strptime(raw.strip(), "%H:%M").time()
    except ValueError as exc:
        raise ConfigError(f"{name} must be in HH:MM format, got {raw!r}") from exc


def _parse_timezone(name: str, *, default: str) -> str:
    """Validate that an env var names a known IANA timezone; return it as-is."""
    raw = os.getenv(name, default).strip()
    try:
        ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"{name} must be a valid IANA timezone, got {raw!r}") from exc
    return raw
