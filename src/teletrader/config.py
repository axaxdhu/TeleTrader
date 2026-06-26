"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
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
    trade_quantity: int
    market_open: time
    market_close: time
    market_timezone: str

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
            trade_quantity=_parse_int("TRADE_QUANTITY", default=15),
            market_open=_parse_time("MARKET_OPEN_TIME", default="09:15"),
            market_close=_parse_time("MARKET_CLOSE_TIME", default="15:30"),
            market_timezone=_parse_timezone("MARKET_TIMEZONE", default="Asia/Kolkata"),
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
