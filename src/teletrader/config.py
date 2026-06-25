"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


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
