"""Unit tests for the unattended daily token refresh.

This replaces a manual step that was missed eight days running, each miss
permanently destroying that day's scoring. So the tests care most about the
failure paths: it runs unattended before the market opens, and a failure has to
be *reported* clearly enough to act on rather than vanish into a timer's exit
status.

No network: the sender is injected.
"""

from __future__ import annotations

import pytest

from teletrader.fyers_auth import REFRESH_URL, app_id_hash, refresh_access_token


class FakePost:
    """Records the request and returns a canned reply, or raises."""

    def __init__(self, reply: object = None, *, error: Exception | None = None) -> None:
        self._reply = reply
        self._error = error
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url: str, payload: dict) -> object:
        self.calls.append((url, payload))
        if self._error is not None:
            raise self._error
        return self._reply


def _refresh(post: FakePost, **overrides: object):
    params: dict[str, object] = {
        "app_id": "APP-100",
        "secret_id": "secret",
        "refresh_token": "refresh-abc",
        "pin": "1234",
    }
    params.update(overrides)
    return refresh_access_token(post=post, **params)  # type: ignore[arg-type]


# --- The happy path -----------------------------------------------------------


def test_a_fresh_access_token_is_returned() -> None:
    post = FakePost({"s": "ok", "access_token": "new-token"})

    result = _refresh(post)

    assert result.ok is True
    assert result.access_token == "new-token"


def test_the_request_is_what_fyers_expects() -> None:
    post = FakePost({"s": "ok", "access_token": "t"})

    _refresh(post)

    url, payload = post.calls[0]
    assert url == REFRESH_URL
    assert payload["grant_type"] == "refresh_token"
    assert payload["refresh_token"] == "refresh-abc"
    assert payload["pin"] == "1234"
    # The app id and secret are sent hashed, never in the clear.
    assert payload["appIdHash"] == app_id_hash("APP-100", "secret")
    assert "secret" not in str(payload["appIdHash"])


def test_the_hash_is_sha256_of_app_and_secret() -> None:
    import hashlib

    assert app_id_hash("A", "B") == hashlib.sha256(b"A:B").hexdigest()


# --- Failures, which is what this is really about -----------------------------


@pytest.mark.parametrize(
    "missing", ["app_id", "secret_id", "refresh_token", "pin"]
)
def test_missing_configuration_is_named_not_attempted(missing: str) -> None:
    post = FakePost({"s": "ok", "access_token": "t"})

    result = _refresh(post, **{missing: None})

    assert result.ok is False
    assert "not configured" in result.detail
    assert missing.upper() in result.detail.replace("FYERS_", "")
    assert post.calls == []  # nothing is sent half-configured


def test_a_lapsed_refresh_token_says_a_login_is_needed() -> None:
    post = FakePost({"s": "error", "message": "refresh token expired"})

    result = _refresh(post)

    assert result.ok is False
    # Retrying cannot fix this, and saying so is the difference between a
    # five-minute fix and another silent week.
    assert result.needs_relogin is True


def test_a_transport_failure_is_not_blamed_on_the_token() -> None:
    result = _refresh(FakePost(error=TimeoutError("connection timed out")))

    assert result.ok is False
    assert result.needs_relogin is False
    assert "could not reach FYERS" in result.detail


def test_an_error_without_a_message_still_reports_something() -> None:
    result = _refresh(FakePost({"s": "error", "code": -99}))

    assert result.ok is False
    assert result.detail.strip() != ""


def test_an_unreadable_reply_is_a_failure_not_a_crash() -> None:
    result = _refresh(FakePost("not a dict"))

    assert result.ok is False
    assert "unreadable" in result.detail


def test_an_empty_access_token_is_not_treated_as_success() -> None:
    result = _refresh(FakePost({"s": "ok", "access_token": ""}))

    assert result.ok is False


def test_credentials_never_appear_in_the_detail() -> None:
    result = _refresh(FakePost({"s": "error", "message": "nope"}))

    assert "1234" not in result.detail
    assert "refresh-abc" not in result.detail
    assert "secret" not in result.detail


def test_not_being_set_up_is_its_own_case() -> None:
    # Distinct from both a lapsed token and a network fault: nothing is broken,
    # so advising a retry or a re-login would send the reader chasing a problem
    # that does not exist.
    result = _refresh(FakePost(None), pin=None)

    assert result.not_configured is True
    assert result.needs_relogin is False


def test_a_real_failure_is_not_mistaken_for_missing_config() -> None:
    result = _refresh(FakePost({"s": "error", "message": "refresh token expired"}))

    assert result.not_configured is False
    assert result.needs_relogin is True
