"""Unit tests for the pre-market token check.

The check exists because a dead FYERS token failed silently through two days of
live running. So the tests care about two things above all: that a rejected
token is reported as such with what to do about it, and that a healthy one says
**nothing** — a warning that fires every morning is one nobody reads, and then a
real one is missed too.

No network: the client is a fake.
"""

from __future__ import annotations

import pytest

from teletrader.token_check import TokenStatus, format_token_alert, probe_token


class FakeClient:
    """Returns a canned funds response, or raises."""

    def __init__(self, response: object = None, *, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.calls = 0

    def funds(self) -> object:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._response


class NoFundsClient:
    """A client with no funds endpoint at all."""


def _healthy() -> dict[str, object]:
    return {
        "s": "ok",
        "fund_limit": [{"id": 10, "title": "Available Balance", "equityAmount": 50_000.0}],
    }


def _expired() -> dict[str, object]:
    """The real response FYERS gives for an expired daily token."""
    return {"code": -16, "message": "Could not authenticate the user", "s": "error"}


# --- Probing ------------------------------------------------------------------


def test_a_working_token_passes() -> None:
    status = probe_token(FakeClient(_healthy()))

    assert status.ok is True
    assert status.expired is False


def test_an_expired_token_is_reported_with_the_brokers_words() -> None:
    status = probe_token(FakeClient(_expired()))

    assert status.ok is False
    assert status.expired is True
    assert "Could not authenticate the user" in status.detail


def test_a_transport_failure_is_not_called_an_expired_token() -> None:
    # Telling someone to re-authenticate because of a network blip wastes their
    # morning; the two are kept distinct.
    status = probe_token(FakeClient(error=TimeoutError("connection timed out")))

    assert status.ok is False
    assert status.expired is False
    assert "connection timed out" in status.detail


def test_a_client_without_funds_fails_the_check() -> None:
    status = probe_token(NoFundsClient())

    assert status.ok is False
    assert "no funds endpoint" in status.detail


def test_authenticated_but_unreadable_response_is_not_a_pass() -> None:
    # The token works but the funds check would still come back empty, so
    # calling this healthy would recreate the original silent failure.
    status = probe_token(FakeClient({"s": "ok", "fund_limit": [{"title": "Total Balance"}]}))

    assert status.ok is False
    assert "no available balance" in status.detail


def test_the_probe_only_reads() -> None:
    client = FakeClient(_healthy())
    probe_token(client)

    assert client.calls == 1
    # Nothing else is called: a pre-market check must never touch orders.
    assert not hasattr(client, "place_order")


# --- Alerting -----------------------------------------------------------------


def test_a_healthy_token_sends_nothing() -> None:
    # Silence on success is the point: a daily alert stops being read.
    assert format_token_alert(TokenStatus(True, "token accepted")) is None


def test_an_expired_token_alert_says_what_to_do() -> None:
    alert = format_token_alert(
        TokenStatus(False, "Could not authenticate the user"), market_open="09:15"
    )

    assert alert is not None
    assert "Could not authenticate the user" in alert
    assert "09:15" in alert
    assert "fyers_login.py --manual" in alert


def test_a_transient_failure_does_not_demand_a_login() -> None:
    alert = format_token_alert(TokenStatus(False, "could not reach FYERS: timeout"))

    assert alert is not None
    assert "transient" in alert
    assert "fyers_login.py" not in alert


def test_the_alert_says_signals_are_unaffected() -> None:
    # Reassurance that matters: a dead token does not stop the payload alerts,
    # and panicking about a lost day would be the wrong reaction.
    alert = format_token_alert(TokenStatus(False, "Could not authenticate the user"))

    assert alert is not None
    assert "Signal alerts are unaffected" in alert


# --- Surviving the session ----------------------------------------------------
#
# FYERS expires tokens at a fixed 06:00 IST cutoff rather than a fixed age, so a
# token minted at 01:00 is accepted when it is made and dead three hours before
# the market opens. "Does it work now" is therefore the wrong question — one
# made overnight passes it and the session still runs blind.

import base64  # noqa: E402
import json  # noqa: E402
from datetime import datetime, time, timezone  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

from teletrader.token_check import token_expiry  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


def _token(expires: datetime) -> str:
    """A JWT-shaped token whose payload declares ``expires``."""
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(expires.timestamp())}).encode()
    ).decode().rstrip("=")
    return f"header.{payload}.signature"


def test_expiry_is_read_from_the_token_itself() -> None:
    expires = datetime(2026, 10, 1, 6, 0, tzinfo=IST)

    assert token_expiry(_token(expires), tz=IST) == expires


@pytest.mark.parametrize("token", ["", None, "not-a-jwt", "a.b", "a.!!!.c"])
def test_an_unreadable_token_has_no_declared_expiry(token: str | None) -> None:
    # Never raises: an unreadable token simply cannot state its expiry, and the
    # broker's verdict still decides whether it works.
    assert token_expiry(token) is None


def test_a_token_dying_before_the_close_fails_even_though_it_works() -> None:
    # The exact case that cost two sessions: minted at 01:00, valid until 06:00,
    # accepted by the broker right now, useless by 09:15.
    status = probe_token(
        FakeClient(_healthy()),
        token=_token(datetime(2026, 10, 1, 6, 0, tzinfo=IST)),
        market_close=time(15, 30),
        now=datetime(2026, 10, 1, 5, 0, tzinfo=IST),
        tz=IST,
    )

    assert status.ok is False
    assert status.expires_early is True
    assert "will not last the session" in status.detail


def test_a_token_lasting_past_the_close_passes() -> None:
    status = probe_token(
        FakeClient(_healthy()),
        token=_token(datetime(2026, 10, 1, 23, 59, tzinfo=IST)),
        market_close=time(15, 30),
        now=datetime(2026, 10, 1, 8, 45, tzinfo=IST),
        tz=IST,
    )

    assert status.ok is True
    assert status.expires_at is not None


def test_a_token_with_no_declared_expiry_is_not_failed_for_it() -> None:
    # Absence of an expiry claim is not evidence of a short life; the broker's
    # acceptance is what counts.
    status = probe_token(
        FakeClient(_healthy()), token="opaque", market_close=time(15, 30), tz=IST
    )

    assert status.ok is True


def test_the_early_expiry_alert_explains_the_cutoff_not_just_the_symptom() -> None:
    status = probe_token(
        FakeClient(_healthy()),
        token=_token(datetime(2026, 10, 1, 6, 0, tzinfo=IST)),
        market_close=time(15, 30),
        now=datetime(2026, 10, 1, 5, 0, tzinfo=IST),
        tz=IST,
    )
    alert = format_token_alert(status, market_open="09:15")

    assert alert is not None
    assert "06:00 IST cutoff" in alert
    assert "accepts it at the moment" in alert
    assert "Token expiry:" in alert
