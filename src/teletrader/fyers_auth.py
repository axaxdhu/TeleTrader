"""Minting a daily FYERS access token from a refresh token — no browser.

The daily login was the system's weakest part: a three-hour window every morning
that, when missed, silently and permanently destroyed that day's scoring. It was
missed eight days running. A reminder was never going to fix that; removing the
daily step is.

FYERS issues a **refresh token** alongside each access token, valid for about
fifteen days, which can mint a new daily access token without a browser. The
exchange needs the account PIN, which is why this was rejected in July — it means
the PIN lives on the server. That is a real cost, weighed against losing data:
the trade is the user's to make, so nothing here runs unless ``FYERS_PIN`` and
``FYERS_REFRESH_TOKEN`` are both set.

What the PIN here can and cannot do is worth being precise about. It is used
**only** against the refresh endpoint and never stored anywhere but ``.env``; but
a refresh token plus a PIN mints a full trading token, so the file is exactly as
sensitive as the access token already in it. Keep it ``chmod 600``.

This module does the exchange and nothing else — no scheduling, no file writing —
so it can be tested without a network or a clock.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from .logging_config import get_logger

__all__ = ["RefreshResult", "app_id_hash", "refresh_access_token"]

logger = get_logger(__name__)

#: FYERS's refresh endpoint (v3).
REFRESH_URL = "https://api-t1.fyers.in/api/v3/validate-refresh-token"

#: Sends a JSON body to a URL and returns the decoded response.
Poster = Callable[[str, dict[str, Any]], Any]


@dataclass(frozen=True, slots=True)
class RefreshResult:
    """The outcome of a refresh attempt."""

    access_token: str | None
    detail: str

    @property
    def ok(self) -> bool:
        return self.access_token is not None

    @property
    def not_configured(self) -> bool:
        """Whether the refresh was never set up, as opposed to having failed.

        A distinct case from both a lapsed token and a network fault: nothing is
        broken, the feature simply is not enabled, and telling someone to retry
        or re-login would send them chasing a problem that does not exist.
        """
        return not self.ok and self.detail.startswith("not configured")

    @property
    def needs_relogin(self) -> bool:
        """Whether this failure needs a browser login rather than a retry.

        A refresh token lasts about a fortnight; once it lapses no amount of
        retrying helps, and saying so is the difference between a five-minute
        fix and another silent week.
        """
        if self.not_configured:
            return False
        lowered = self.detail.lower()
        return any(
            phrase in lowered
            for phrase in ("refresh token", "expired", "invalid", "authenticate")
        )


def app_id_hash(app_id: str, secret_id: str) -> str:
    """Return the ``appIdHash`` FYERS expects: SHA-256 of ``app_id:secret_id``."""
    return hashlib.sha256(f"{app_id}:{secret_id}".encode()).hexdigest()


def refresh_access_token(
    *,
    app_id: str | None,
    secret_id: str | None,
    refresh_token: str | None,
    pin: str | None,
    post: Poster | None = None,
) -> RefreshResult:
    """Exchange a refresh token for a fresh daily access token.

    Returns a result rather than raising: this runs unattended before the market
    opens, and a failure has to be *reported* (so the user can log in manually)
    rather than crash a timer and vanish into a unit's exit status.

    Credentials are never logged, including on failure.
    """
    missing = [
        name
        for name, value in (
            ("FYERS_APP_ID", app_id),
            ("FYERS_SECRET_ID", secret_id),
            ("FYERS_REFRESH_TOKEN", refresh_token),
            ("FYERS_PIN", pin),
        )
        if not value
    ]
    if missing:
        return RefreshResult(None, f"not configured: {', '.join(missing)} not set")

    assert app_id and secret_id and refresh_token and pin  # narrowed by the check
    payload = {
        "grant_type": "refresh_token",
        "appIdHash": app_id_hash(app_id, secret_id),
        "refresh_token": refresh_token,
        "pin": pin,
    }

    try:
        response = (post or _post_json)(REFRESH_URL, payload)
    except Exception as exc:  # noqa: BLE001 — a transport failure is a result, not a crash
        logger.warning("FYERS refresh call failed: %s", exc)
        return RefreshResult(None, f"could not reach FYERS: {exc}")

    if not isinstance(response, dict):
        return RefreshResult(None, "unreadable response from FYERS")

    token = response.get("access_token")
    if isinstance(token, str) and token:
        return RefreshResult(token, "refreshed")

    # FYERS reports failure in the body, as it does everywhere else.
    message = str(response.get("message") or "").strip()
    return RefreshResult(None, message or f"FYERS refused the refresh: {response}")


def _post_json(url: str, payload: dict[str, Any]) -> Any:
    """POST ``payload`` as JSON and decode the reply (production sender)."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as reply:  # noqa: S310 — fixed https URL
            return json.loads(reply.read().decode())
    except urllib.error.HTTPError as exc:
        # FYERS returns its error body with a 4xx; that body is the useful part.
        try:
            return json.loads(exc.read().decode())
        except Exception:  # noqa: BLE001
            raise
