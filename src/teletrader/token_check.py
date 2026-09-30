"""Pre-market check that the daily broker token is actually alive.

The FYERS access token expires every day, and nothing notices until a signal
arrives and the funds check quietly fails — which is exactly how a dead token
ran through two days of live shadowing unremarked. This module asks the broker,
before the session starts, whether the token still works, so the answer arrives
while there is still time to do something about it.

It only ever *reads* (the funds endpoint), and it alerts on failure only: a
healthy token sends nothing, because a notification that arrives every single
morning is one nobody reads.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime, time, timezone, tzinfo
from typing import Any

from .execution.shadow import _available_from_funds, _funds_error
from .logging_config import get_logger

__all__ = [
    "TokenStatus",
    "format_token_alert",
    "probe_token",
    "token_expiry",
]

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TokenStatus:
    """Whether the broker accepted the token, and what it said if not."""

    ok: bool
    detail: str
    #: When the token stops being accepted, if it says so. FYERS issues a JWT
    #: whose ``exp`` claim is a fixed **06:00 IST** cutoff — not 24 hours from
    #: issue — so a token minted overnight can be hours from death when it is
    #: created.
    expires_at: datetime | None = None

    @property
    def expired(self) -> bool:
        """Whether the broker actively rejected the token (vs. a transport fault).

        Worth separating: a rejected token needs a login, while a network blip
        needs nothing but patience, and telling someone to re-authenticate over a
        timeout wastes their morning.
        """
        return not self.ok and "could not authenticate" in self.detail.lower()

    @property
    def expires_early(self) -> bool:
        """Whether the token works now but dies before the session ends.

        Its own fault to fix — the same login solves it — so it is grouped with
        an expired token when advising what to do.
        """
        return not self.ok and "will not last the session" in self.detail


def token_expiry(token: str | None, *, tz: tzinfo = timezone.utc) -> datetime | None:
    """Read a FYERS access token's own expiry, or ``None`` if it does not say.

    The token is a JWT whose payload carries an ``exp`` claim. Only that claim is
    read; the signature is not verified, because this is not an authorisation
    decision — the broker makes that. It is simply the token stating when it
    stops working, which is far better than guessing.
    """
    if not token:
        return None
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    try:
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        claims = json.loads(raw)
        expires = int(claims["exp"])
    except (binascii.Error, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return datetime.fromtimestamp(expires, tz=timezone.utc).astimezone(tz)


def probe_token(
    client: Any,
    *,
    token: str | None = None,
    market_close: time | None = None,
    now: datetime | None = None,
    tz: tzinfo = timezone.utc,
) -> TokenStatus:
    """Ask the broker whether the token works — and whether it will last the day.

    Uses the funds endpoint because it is read-only, cheap, and the same call the
    shadow executor depends on, so a pass here means the thing that failed
    silently before will now work.

    "Works right now" is not the question that matters, though. FYERS tokens
    expire at a fixed **06:00 IST** cutoff rather than a fixed age, so one minted
    at 01:00 is accepted when it is made and dead three hours before the market
    opens. When the token declares an expiry and ``market_close`` is known, a
    token that will not survive the session is reported as a failure even though
    the broker is currently accepting it — otherwise the check passes at 08:45
    and the session still runs blind.
    """
    funds = getattr(client, "funds", None)
    if not callable(funds):
        return TokenStatus(False, "this client has no funds endpoint to check")

    try:
        response = funds()
    except Exception as exc:  # noqa: BLE001 — a check must never take the box down
        logger.warning("Token check could not reach FYERS: %s", exc)
        return TokenStatus(False, f"could not reach FYERS: {exc}")

    error = _funds_error(response)
    if error is not None:
        return TokenStatus(False, error, token_expiry(token, tz=tz))

    if _available_from_funds(response) is None:
        # Authenticated, but the response is not what the funds check expects —
        # worth flagging rather than calling the token healthy.
        return TokenStatus(
            False,
            "authenticated, but no available balance in the response",
            token_expiry(token, tz=tz),
        )

    expires_at = token_expiry(token, tz=tz)
    if expires_at is not None and market_close is not None:
        moment = (now or datetime.now(timezone.utc)).astimezone(tz)
        closes_at = moment.replace(
            hour=market_close.hour, minute=market_close.minute, second=0, microsecond=0
        )
        if expires_at < closes_at:
            return TokenStatus(
                False,
                f"token expires at {expires_at:%H:%M}, before the {market_close:%H:%M} "
                "close — it will not last the session",
                expires_at,
            )

    return TokenStatus(True, "token accepted", expires_at)


def format_token_alert(status: TokenStatus, *, market_open: str = "09:15") -> str | None:
    """Render the pre-market warning, or ``None`` when nothing needs saying.

    Silence on success is deliberate: an alert that fires every morning stops
    being read, and then a real one is missed too.
    """
    if status.ok:
        return None

    lines = ["⚠️ FYERS token check FAILED", status.detail, ""]
    if status.expires_at is not None:
        lines.append(f"Token expiry: {status.expires_at:%Y-%m-%d %H:%M} (it says so itself)")
        lines.append("")
    if status.expires_early:
        # Distinct from an expired token: the broker accepts it *now*, which is
        # exactly why this would otherwise pass unnoticed until mid-session.
        lines.append(
            "The broker accepts it at the moment, but it dies before the close. "
            "FYERS tokens expire at a fixed 06:00 IST cutoff rather than a fixed "
            f"age, so one made overnight is already doomed. Log in again after "
            f"06:00 and before {market_open}:"
        )
        lines.append("  uv run python fyers_login.py --manual")
    elif status.expired:
        lines.append(
            f"The daily token has expired. Log in before {market_open} or today "
            "will have no funds check and no end-of-day P&L:"
        )
        lines.append("  uv run python fyers_login.py --manual")
    else:
        lines.append(
            "This may be a transient fault rather than the token — it is worth "
            "re-checking before logging in again."
        )
    lines.append("")
    lines.append(
        "Signal alerts are unaffected: contract, expiry, quantity and the "
        "protective exits all still arrive."
    )
    return "\n".join(lines)
