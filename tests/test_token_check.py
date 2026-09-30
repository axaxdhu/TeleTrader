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
