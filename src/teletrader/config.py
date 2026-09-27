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
    # --- Second signal channel (parse-only) ----------------------------------
    # An optional second channel whose messages are parsed and stored but not
    # traded (see teletrader.channel2). Each channel can be enabled/disabled
    # independently; a channel with no id configured is simply not listened to.
    channel_2: int | str | None = None
    channel_1_enabled: bool = True
    channel_2_enabled: bool = True
    # --- Live broker: FYERS --------------------------------------------------
    # Only required when a channel is routed to the ``fyers`` broker. Like Kite,
    # authentication is manual: a valid daily access token is assumed to already
    # exist (this app never logs in nor generates tokens; see ``fyers_login.py``).
    # Secrets, never logged.
    fyers_app_id: str | None = None
    fyers_secret_id: str | None = None
    fyers_access_token: str | None = None
    # --- Per-channel broker selection ----------------------------------------
    # Each channel can route to its own broker independently. An unset override
    # falls back to ``execution_mode`` (the global default), so existing
    # single-broker setups keep working. Resolve via :meth:`broker_for`.
    channel_1_broker: str | None = None
    channel_2_broker: str | None = None
    # --- Telegram bot notifications ------------------------------------------
    # Optional push alerts (recognised signal + outcome) sent to you via a
    # Telegram *bot* — a separate sender, so your phone actually notifies (unlike
    # messaging your own account). Off by default; the token is a secret (never
    # logged / committed). See teletrader.notifier.
    notify_enabled: bool = False
    notify_bot_token: str | None = None
    notify_chat_id: str | None = None

    def broker_for(self, channel_name: str) -> str:
        """Return the broker mode a channel should use (its own or the default).

        A per-channel override (``CHANNEL_1_BROKER`` / ``CHANNEL_2_BROKER``) wins;
        otherwise the channel uses ``execution_mode``. This is the one place the
        per-channel routing is resolved, so the composition root can ask each
        channel for *its* broker and build the matching executor.
        """
        overrides = {
            "channel1": self.channel_1_broker,
            "channel2": self.channel_2_broker,
        }
        return overrides.get(channel_name) or self.execution_mode

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
        channel_1_broker = _parse_optional_execution_mode("CHANNEL_1_BROKER")
        channel_2_broker = _parse_optional_execution_mode("CHANNEL_2_BROKER")

        # Credentials are required for any broker actually referenced (the global
        # default or a per-channel override), so a misconfigured live broker fails
        # fast at startup rather than at the first order.
        referenced = {execution_mode, channel_1_broker, channel_2_broker} - {None}
        kite_creds = _parse_kite_credentials(referenced)
        fyers_creds = _parse_fyers_credentials(referenced)

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
            **kite_creds,
            channel_2=_parse_optional_channel("TELEGRAM_CHANNEL_2"),
            channel_1_enabled=_parse_bool("CHANNEL_1_ENABLED", default=True),
            channel_2_enabled=_parse_bool("CHANNEL_2_ENABLED", default=True),
            **fyers_creds,
            channel_1_broker=channel_1_broker,
            channel_2_broker=channel_2_broker,
            notify_enabled=_parse_bool("NOTIFY_ENABLED", default=False),
            notify_bot_token=os.getenv("NOTIFY_BOT_TOKEN") or None,
            notify_chat_id=os.getenv("NOTIFY_CHAT_ID") or None,
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


def _parse_optional_channel(name: str) -> int | str | None:
    """Parse an *optional* channel identifier env var (``None`` when unset).

    Same numeric-vs-username coercion as :func:`_parse_channel`, but the variable
    is not required — an absent second channel simply means it is not listened to.
    """
    value = os.getenv(name)
    if not value or not value.strip():
        return None
    return _parse_channel(value)


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


#: Order-execution modes / broker selectors. ``dry_run`` validates + logs without
#: sending; ``kite`` and ``fyers`` place live orders; ``fyers_shadow`` walks the
#: entire live FYERS path — real symbol, expiry, lot size and funds check — but
#: stops immediately before submitting, so it proves an order *would* be accepted
#: without risking money. A channel selects one of these (globally via
#: ``EXECUTION_MODE`` or per-channel via ``CHANNEL_N_BROKER``); the factory maps
#: the chosen mode to an executor.
_EXECUTION_MODES = frozenset({"dry_run", "kite", "fyers", "fyers_shadow"})

#: Modes that talk to FYERS and therefore need FYERS credentials. ``fyers_shadow``
#: places no order but still calls the API (funds check), so it needs a valid
#: daily token exactly like the live mode.
_FYERS_MODES = frozenset({"fyers", "fyers_shadow"})


def _parse_execution_mode(name: str, *, default: str) -> str:
    """Parse and validate an execution-mode env var against the known modes."""
    value = os.getenv(name, default).strip().lower()
    if value not in _EXECUTION_MODES:
        allowed = ", ".join(sorted(_EXECUTION_MODES))
        raise ConfigError(f"{name} must be one of {{{allowed}}}, got {value!r}")
    return value


def _parse_optional_execution_mode(name: str) -> str | None:
    """Parse an *optional* per-channel broker override (``None`` when unset).

    Same validation as :func:`_parse_execution_mode`, but an absent variable means
    "use the global default" (resolved in :meth:`Config.broker_for`).
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    value = raw.strip().lower()
    if value not in _EXECUTION_MODES:
        allowed = ", ".join(sorted(_EXECUTION_MODES))
        raise ConfigError(f"{name} must be one of {{{allowed}}}, got {value!r}")
    return value


def _parse_kite_credentials(referenced_modes: set[str]) -> dict[str, str | None]:
    """Read the Kite credentials, requiring them only when ``kite`` is in use.

    If any channel routes to ``kite`` all three (``KITE_API_KEY``,
    ``KITE_API_SECRET``, ``KITE_ACCESS_TOKEN``) must be set — missing ones fail
    fast at startup. Otherwise they are optional (and usually absent).
    Authentication is manual: the access token is assumed already valid; this app
    never logs in.
    """
    creds = {
        "kite_api_key": os.getenv("KITE_API_KEY") or None,
        "kite_api_secret": os.getenv("KITE_API_SECRET") or None,
        "kite_access_token": os.getenv("KITE_ACCESS_TOKEN") or None,
    }
    if "kite" in referenced_modes:
        _require_credentials(
            "kite",
            creds,
            (
                ("KITE_API_KEY", "kite_api_key"),
                ("KITE_API_SECRET", "kite_api_secret"),
                ("KITE_ACCESS_TOKEN", "kite_access_token"),
            ),
        )
    return creds


def _parse_fyers_credentials(referenced_modes: set[str]) -> dict[str, str | None]:
    """Read the FYERS credentials, requiring them only when ``fyers`` is in use.

    If any channel routes to ``fyers`` all three (``FYERS_APP_ID``,
    ``FYERS_SECRET_ID``, ``FYERS_ACCESS_TOKEN``) must be set — missing ones fail
    fast at startup. Otherwise they are optional. Authentication is manual (a
    valid daily token is assumed; see ``fyers_login.py``).
    """
    creds = {
        "fyers_app_id": os.getenv("FYERS_APP_ID") or None,
        "fyers_secret_id": os.getenv("FYERS_SECRET_ID") or None,
        "fyers_access_token": os.getenv("FYERS_ACCESS_TOKEN") or None,
    }
    if referenced_modes & _FYERS_MODES:
        _require_credentials(
            "fyers",
            creds,
            (
                ("FYERS_APP_ID", "fyers_app_id"),
                ("FYERS_SECRET_ID", "fyers_secret_id"),
                ("FYERS_ACCESS_TOKEN", "fyers_access_token"),
            ),
        )
    return creds


def _require_credentials(
    broker: str,
    creds: dict[str, str | None],
    mapping: tuple[tuple[str, str], ...],
) -> None:
    """Raise :class:`ConfigError` if any credential for ``broker`` is missing."""
    missing = [env for env, field in mapping if creds[field] is None]
    if missing:
        raise ConfigError(
            f"broker '{broker}' requires " + ", ".join(missing) + " to be set"
        )


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
