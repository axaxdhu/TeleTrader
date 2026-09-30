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

from dataclasses import dataclass
from typing import Any

from .execution.shadow import _available_from_funds, _funds_error
from .logging_config import get_logger

__all__ = ["TokenStatus", "format_token_alert", "probe_token"]

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TokenStatus:
    """Whether the broker accepted the token, and what it said if not."""

    ok: bool
    detail: str

    @property
    def expired(self) -> bool:
        """Whether the broker actively rejected the token (vs. a transport fault).

        Worth separating: a rejected token needs a login, while a network blip
        needs nothing but patience, and telling someone to re-authenticate over a
        timeout wastes their morning.
        """
        return not self.ok and "could not authenticate" in self.detail.lower()


def probe_token(client: Any) -> TokenStatus:
    """Ask the broker whether the token still works.

    Uses the funds endpoint because it is read-only, cheap, and the same call the
    shadow executor depends on — so a pass here means the thing that failed
    silently before will now work.
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
        return TokenStatus(False, error)

    if _available_from_funds(response) is None:
        # Authenticated, but the response is not what the funds check expects —
        # worth flagging rather than calling the token healthy.
        return TokenStatus(False, "authenticated, but no available balance in the response")

    return TokenStatus(True, "token accepted")


def format_token_alert(status: TokenStatus, *, market_open: str = "09:15") -> str | None:
    """Render the pre-market warning, or ``None`` when nothing needs saying.

    Silence on success is deliberate: an alert that fires every morning stops
    being read, and then a real one is missed too.
    """
    if status.ok:
        return None

    lines = ["⚠️ FYERS token check FAILED", status.detail, ""]
    if status.expired:
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
