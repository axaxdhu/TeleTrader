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
