"""Unit tests for the Telegram-bot notifier and alert formatting.

No network: the low-level sender is injected. Covers alert formatting for each
pipeline outcome (recognised signal + result, incl. dry-run and not-traded),
best-effort delivery (a send failure is swallowed), and the enabled/disabled
factory.
"""

from __future__ import annotations

from datetime import time

import pytest

from teletrader.commands import parse_command
from teletrader.config import Config
from teletrader.execution import (
    ExecutionResult,
    ExecutionStatus,
    OrderRequest,
    OrderType,
    TransactionType,
)
from teletrader.notifier import (
    NullNotifier,
    TelegramBotNotifier,
    create_notifier,
    format_alert,
)
from teletrader.parser import parse_signal
from teletrader.pipeline import PipelineResult, PipelineStatus
from teletrader.trade_engine import TradeDecision

SIGNAL = parse_signal("NIFTY 23900 PE ABOVE 165\n\nSL-150\n\nTGT-200")


def _order() -> OrderRequest:
    return OrderRequest(
        symbol="NIFTY 23900 PE",
        transaction_type=TransactionType.BUY,
        quantity=75,
        order_type=OrderType.MARKET,
        entry_price=165.0,
        stop_loss=150.0,
        target=198.0,
    )


def _execution(status: ExecutionStatus, *, broker_order_id: str | None = None) -> ExecutionResult:
    return ExecutionResult(
        status=status, order=_order(), remarks="ok", broker_order_id=broker_order_id
    )


def _config(**overrides: object) -> Config:
    defaults = dict(
        api_id=1, api_hash="h", phone="+1", session_name="t", channel="me",
        log_level="INFO", database_path=":memory:", auto_trading=True,
        allow_duplicates=False, max_trades_per_day=100, trade_lots=1,
        lot_sizes={"NIFTY": 75}, market_open=time(9, 15), market_close=time(15, 30),
        market_timezone="Asia/Kolkata", execution_mode="dry_run",
        kite_api_key=None, kite_api_secret=None, kite_access_token=None,
    )
    defaults.update(overrides)
    return Config(**defaults)  # type: ignore[arg-type]


# --- Alert formatting ---------------------------------------------------------


def test_ignored_is_not_alerted() -> None:
    result = PipelineResult(PipelineStatus.IGNORED)
    assert format_alert(result, channel_name="channel1", broker="dry_run") is None


def test_dry_run_execution_is_alerted_as_would_place() -> None:
    result = PipelineResult(
        PipelineStatus.EXECUTED, signal=SIGNAL, stored_id=7,
        execution=_execution(ExecutionStatus.SUCCESS),
    )
    alert = format_alert(result, channel_name="channel1", broker="dry_run")
    assert alert is not None
    assert "DRY RUN" in alert
    assert "NIFTY 23900 PE" in alert
    assert "Broker: dry_run" in alert


def test_live_execution_is_alerted_as_order_placed() -> None:
    result = PipelineResult(
        PipelineStatus.EXECUTED, signal=SIGNAL, stored_id=7,
        execution=_execution(ExecutionStatus.SUCCESS, broker_order_id="OID9"),
    )
    alert = format_alert(result, channel_name="channel1", broker="fyers")
    assert "ORDER PLACED" in alert
    assert "OID9" in alert
    assert "Broker: fyers" in alert


def test_rejected_execution_is_alerted_as_failure() -> None:
    result = PipelineResult(
        PipelineStatus.EXECUTED, signal=SIGNAL, stored_id=7,
        execution=_execution(ExecutionStatus.REJECTED),
    )
    alert = format_alert(result, channel_name="channel1", broker="kite")
    assert "❌" in alert
    assert "rejected" in alert.lower()


def test_not_traded_is_alerted_with_reason() -> None:
    result = PipelineResult(
        PipelineStatus.NOT_TRADED, signal=SIGNAL, stored_id=7,
        decision=TradeDecision(execute=False, quantity=0, reason="Auto-trading disabled"),
    )
    alert = format_alert(result, channel_name="channel1", broker="dry_run")
    assert "NOT traded" in alert
    assert "Auto-trading disabled" in alert


def test_duplicate_is_alerted() -> None:
    result = PipelineResult(PipelineStatus.DUPLICATE, signal=SIGNAL)
    alert = format_alert(result, channel_name="channel1", broker="dry_run")
    assert "Duplicate" in alert


def test_stored_parse_only_is_alerted() -> None:
    result = PipelineResult(PipelineStatus.STORED, signal=SIGNAL, stored_id=3)
    alert = format_alert(result, channel_name="channel2", broker="dry_run")
    assert "parse-only" in alert
    assert "[channel2]" in alert


def test_management_command_is_alerted() -> None:
    command = parse_command("Avoid")
    assert command is not None
    result = PipelineResult(PipelineStatus.CANCELLED, command=command)
    alert = format_alert(result, channel_name="channel1", broker="dry_run")
    assert "COMMAND" in alert
    assert "cancelled" in alert.lower()


# --- Delivery (best-effort) ---------------------------------------------------


def test_notifier_sends_to_the_configured_chat() -> None:
    sent: list[tuple[str, str]] = []
    notifier = TelegramBotNotifier("TOKEN", "12345", sender=lambda url, payload: sent.append((url, payload)))
    notifier.notify("hello")

    (url, payload) = sent[0]
    assert "botTOKEN" in url and url.endswith("/sendMessage")
    assert '"chat_id": "12345"' in payload
    assert '"text": "hello"' in payload


def test_notifier_swallows_send_failures() -> None:
    def boom(url: str, payload: str) -> None:
        raise ConnectionError("telegram down")

    notifier = TelegramBotNotifier("T", "1", sender=boom)
    notifier.notify("x")  # must not raise


# --- Factory ------------------------------------------------------------------


def test_create_notifier_disabled_by_default() -> None:
    assert isinstance(create_notifier(_config(notify_enabled=False)), NullNotifier)


def test_create_notifier_enabled_but_unconfigured_is_null() -> None:
    notifier = create_notifier(_config(notify_enabled=True, notify_bot_token=None))
    assert isinstance(notifier, NullNotifier)


def test_create_notifier_enabled_and_configured() -> None:
    notifier = create_notifier(
        _config(notify_enabled=True, notify_bot_token="T", notify_chat_id="1")
    )
    assert isinstance(notifier, TelegramBotNotifier)


def test_null_notifier_is_a_noop() -> None:
    NullNotifier().notify("anything")  # must not raise


# --- Shadow + missed alerts ---------------------------------------------------
#
# During the ramp-up these two alerts are the entire feedback loop: the user is
# at work and sees only what the bot sends. So the shadow alert has to carry
# enough to judge a would-be order, and a parse miss must never look like
# silence.

from datetime import date as _date  # noqa: E402

from teletrader.execution import ShadowLeg, ShadowReport  # noqa: E402


def _shadow_report(**overrides: object) -> ShadowReport:
    params: dict[str, object] = {
        "tradingsymbol": "NSE:COFORGE26OCT1500CE",
        "exchange": "NSE",
        "expiry": _date(2026, 10, 29),
        "lot_size": 150,
        "lots": 1,
        "quantity": 150,
        "payload": {"symbol": "NSE:COFORGE26OCT1500CE", "qty": 150},
        "funds_required": 10_500.0,
        "funds_available": 50_000.0,
        "funds_ok": True,
        "funds_note": "Sufficient funds.",
        "protective": (
            ShadowLeg("stop-loss", "SL-M", 66.0, {"stopPrice": 66.0}, True, "rests at 66"),
            ShadowLeg("target", "LIMIT", 73.0, {"limitPrice": 73.0}, True, "rests at 73"),
        ),
    }
    params.update(overrides)
    return ShadowReport(**params)  # type: ignore[arg-type]


def _shadow_result(
    status: ExecutionStatus = ExecutionStatus.SUCCESS, **overrides: object
) -> PipelineResult:
    return PipelineResult(
        PipelineStatus.SHADOWED,
        signal=SIGNAL,
        stored_id=11,
        decision=TradeDecision(execute=True, quantity=150, reason="Signal accepted"),
        execution=ExecutionResult(
            status=status,
            order=_order(),
            remarks="[SHADOW] Would submit to FYERS.",
            shadow=_shadow_report(**overrides),
        ),
    )


def _alert(result: PipelineResult, broker: str = "fyers_shadow") -> str:
    text = format_alert(result, channel_name="channel2", broker=broker)
    assert text is not None
    return text


def test_shadow_alert_carries_the_contract_and_sizing() -> None:
    text = _alert(_shadow_result())

    assert "NSE:COFORGE26OCT1500CE" in text
    assert "2026-10-29" in text          # expiry
    assert "150 (1 lot x 150)" in text   # quantity and how it was derived


def test_shadow_alert_says_no_order_was_sent() -> None:
    text = _alert(_shadow_result())

    # The user must never mistake a shadow alert for a real fill.
    assert "SHADOW" in text
    assert "no order sent" in text.lower()


def test_shadow_alert_shows_the_verdict_and_funds() -> None:
    text = _alert(_shadow_result())

    assert "WOULD GO THROUGH" in text
    assert "10500" in text or "10,500" in text  # cost
    assert "Sufficient funds." in text


def test_shadow_alert_reports_a_failing_verdict() -> None:
    text = _alert(
        _shadow_result(
            ExecutionStatus.REJECTED,
            funds_ok=False,
            funds_available=1_000.0,
            funds_note="insufficient funds - needs 10500.00, available 1000.00",
        )
    )

    assert "WOULD BE REJECTED" in text
    assert "insufficient funds" in text


def test_shadow_alert_reports_an_unknown_balance_honestly() -> None:
    text = _alert(
        _shadow_result(
            funds_ok=None,
            funds_available=None,
            funds_note="Funds check unavailable (balance not read).",
        )
    )

    assert "unknown" in text
    assert "unavailable" in text


def test_missed_alert_quotes_the_original_message() -> None:
    raw = "Coforge 1500 ce above 70\nsl-- sixty six"
    text = _alert(PipelineResult(PipelineStatus.MISSED, raw_text=raw))

    # The raw text is the point: it is what tells the user what to fix.
    assert "NOT PARSED" in text
    assert "sl-- sixty six" in text
    assert "Nothing was traded" in text


def test_missed_alert_trims_a_very_long_message() -> None:
    text = _alert(PipelineResult(PipelineStatus.MISSED, raw_text="x" * 900))

    assert "…" in text
    assert len(text) < 600


def test_missed_alert_survives_an_empty_message() -> None:
    text = _alert(PipelineResult(PipelineStatus.MISSED, raw_text=None))

    assert "(empty)" in text


def test_shadow_alert_lists_both_protective_exits() -> None:
    text = _alert(_shadow_result())

    assert "Exits (placed after the entry fills)" in text
    assert "stop-loss: SL-M @ 66" in text
    assert "target: LIMIT @ 73" in text


def test_shadow_alert_warns_when_protection_is_incomplete() -> None:
    # An entry that fills and then cannot be protected is not a working trade —
    # reporting a plain "would go through" here would be actively misleading.
    text = _alert(
        _shadow_result(
            protective=(
                ShadowLeg("stop-loss", "SL-M", None, None, False, "No stop-loss in the signal."),
                ShadowLeg("target", "LIMIT", 73.0, {"limitPrice": 73.0}, True, "rests at 73"),
            )
        )
    )

    assert "PROTECTION INCOMPLETE" in text
    assert "WOULD GO THROUGH" not in text
    assert "No stop-loss in the signal." in text
