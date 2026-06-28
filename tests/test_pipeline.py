"""Unit tests for the signal pipeline (parse → store → evaluate → execute).

Exercises the full wiring with real collaborators (repository, engine, dry-run
executor) over an in-memory database and a fixed engine clock — no Telegram. Each
of the four outcomes (ignored / duplicate / not-traded / executed) has a test,
plus the Signal → OrderRequest mapping.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest

from teletrader.config import Config
from teletrader.database import connect, initialize
from teletrader.execution import (
    DryRunExecutor,
    ExecutionRepository,
    ExecutionStatus,
    OrderType,
    TransactionType,
)
from teletrader.parser import Action, OptionType, Signal
from teletrader.pipeline import (
    PipelineStatus,
    SignalPipeline,
    build_order_request,
)
from teletrader.repository import SignalRepository
from teletrader.trade_engine import TradeDecision, TradeEngine

IST = ZoneInfo("Asia/Kolkata")
# A Friday, 10:00 IST — a weekday inside the default 09:15–15:30 window.
TRADING_MOMENT = datetime(2026, 6, 26, 10, 0, tzinfo=IST)

SIGNAL_TEXT = "NIFTY 23900 PE ABOVE 165\n\nSL-150\n\nTGT-200+"


@pytest.fixture
def connection() -> sqlite3.Connection:
    conn = connect(":memory:")
    initialize(conn)
    yield conn
    conn.close()


def _config(**overrides: object) -> Config:
    defaults = dict(
        api_id=1,
        api_hash="hash",
        phone="+10000000000",
        session_name="test",
        channel="me",
        log_level="INFO",
        database_path=":memory:",
        auto_trading=True,
        allow_duplicates=False,
        max_trades_per_day=100,
        trade_lots=1,
        lot_sizes={"NIFTY": 65},
        market_open=time(9, 15),
        market_close=time(15, 30),
        market_timezone="Asia/Kolkata",
        execution_mode="dry_run",
    )
    defaults.update(overrides)
    return Config(**defaults)  # type: ignore[arg-type]


def _pipeline(connection: sqlite3.Connection, config: Config | None = None) -> SignalPipeline:
    config = config or _config()
    signal_repo = SignalRepository(connection, tz=timezone.utc)
    engine = TradeEngine(config, signal_repo, clock=lambda: TRADING_MOMENT)
    executor = DryRunExecutor(ExecutionRepository(connection), clock=lambda: TRADING_MOMENT)
    return SignalPipeline(signal_repo, engine, executor)


# --- The four outcomes --------------------------------------------------------


def test_noise_is_ignored(connection: sqlite3.Connection) -> None:
    result = _pipeline(connection).process("good morning, traders!")
    assert result.status is PipelineStatus.IGNORED
    assert result.signal is None


def test_valid_signal_is_stored_and_executed(connection: sqlite3.Connection) -> None:
    result = _pipeline(connection).process(SIGNAL_TEXT, when=TRADING_MOMENT)
    assert result.status is PipelineStatus.EXECUTED
    assert result.stored_id == 1
    assert result.decision is not None and result.decision.execute is True
    assert result.execution is not None
    assert result.execution.status is ExecutionStatus.SUCCESS
    # The execution attempt was recorded and linked back to the stored signal.
    executions = ExecutionRepository(connection).list_all()
    assert len(executions) == 1
    assert executions[0].signal_id == 1
    assert executions[0].quantity == 65  # 1 lot x NIFTY lot size


def test_duplicate_same_day_is_reported(connection: sqlite3.Connection) -> None:
    pipeline = _pipeline(connection)
    pipeline.process(SIGNAL_TEXT, when=TRADING_MOMENT)
    again = pipeline.process(SIGNAL_TEXT, when=TRADING_MOMENT)
    assert again.status is PipelineStatus.DUPLICATE
    # No second execution attempt was made.
    assert ExecutionRepository(connection).count() == 1


def test_signal_not_traded_when_auto_trading_off(connection: sqlite3.Connection) -> None:
    result = _pipeline(connection, _config(auto_trading=False)).process(
        SIGNAL_TEXT, when=TRADING_MOMENT
    )
    assert result.status is PipelineStatus.NOT_TRADED
    assert result.stored_id == 1  # still stored
    assert result.decision is not None and "Auto-trading disabled" in result.decision.reason
    # The engine declined, so nothing reached the executor.
    assert ExecutionRepository(connection).count() == 0


# --- Signal -> OrderRequest mapping -------------------------------------------


def test_build_order_request_maps_fields() -> None:
    signal = Signal(
        underlying="NIFTY",
        strike=23900,
        option_type=OptionType.PUT,
        action=Action.BUY,
        entry_price=165.0,
        stop_loss=150.0,
        target=198.0,
        target_open_ended=True,
        raw_text=SIGNAL_TEXT,
    )
    decision = TradeDecision(execute=True, quantity=65, reason="Signal accepted")
    order = build_order_request(signal, decision, signal_id=7)
    assert order.symbol == "NIFTY 23900 PE"
    assert order.transaction_type is TransactionType.BUY
    assert order.quantity == 65
    assert order.order_type is OrderType.MARKET
    assert order.entry_price == 165.0
    assert order.stop_loss == 150.0
    assert order.target == 198.0
    assert order.signal_id == 7
