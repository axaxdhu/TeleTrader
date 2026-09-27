"""Unit tests for the order-execution layer (DryRunExecutor + factory).

Covers the areas called out for this phase: a valid order request, an invalid
one, the ``[DRY RUN]`` logging, execution-history persistence, configuration-driven
executor switching, and the exception hierarchy. Everything runs against an
in-memory database and a fixed clock — no broker is ever contacted.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, time, timezone

import pytest

from teletrader.config import Config, ConfigError
from teletrader.database import connect, initialize
from teletrader.execution import (
    DryRunExecutor,
    ExecutionError,
    ExecutionRepository,
    ExecutionResult,
    ExecutionStatus,
    Executor,
    InvalidOrderError,
    ManagementAction,
    OrderRequest,
    OrderType,
    TransactionType,
    create_executor,
    validate_order,
)

NOW = datetime(2026, 6, 29, 10, 0, tzinfo=timezone.utc)


# --- Fixtures / builders ------------------------------------------------------


@pytest.fixture
def connection() -> sqlite3.Connection:
    conn = connect(":memory:")
    initialize(conn)
    yield conn
    conn.close()


@pytest.fixture
def repo(connection: sqlite3.Connection) -> ExecutionRepository:
    return ExecutionRepository(connection)


@pytest.fixture
def executor(repo: ExecutionRepository) -> DryRunExecutor:
    return DryRunExecutor(repo, clock=lambda: NOW)


# --- Management operations ----------------------------------------------------


def test_dry_run_cancel_order_succeeds(executor: DryRunExecutor) -> None:
    result = executor.cancel_order("OID1", symbol="NIFTY 23900 PE")
    assert result.action is ManagementAction.CANCEL
    assert result.status is ExecutionStatus.SUCCESS
    assert result.succeeded


def test_dry_run_modify_stop_loss_succeeds(executor: DryRunExecutor) -> None:
    result = executor.modify_stop_loss("SL1", 165.0, symbol="NIFTY 23900 PE")
    assert result.action is ManagementAction.MODIFY_STOP_LOSS
    assert result.succeeded
    assert "165" in result.remarks


def test_dry_run_modify_target_succeeds(executor: DryRunExecutor) -> None:
    result = executor.modify_target("TG1", 250.0)
    assert result.action is ManagementAction.MODIFY_TARGET
    assert result.succeeded


def test_base_executor_management_ops_default_to_unsupported() -> None:
    """An executor that doesn't override the management ops fails cleanly."""

    class BareExecutor(Executor):
        @property
        def mode(self) -> str:
            return "bare"

        def execute(self, order: OrderRequest) -> ExecutionResult:  # pragma: no cover
            raise NotImplementedError

    bare = BareExecutor()
    result = bare.cancel_order("OID1")
    assert result.status is ExecutionStatus.FAILED
    assert "not supported" in result.remarks
    assert "bare" in result.remarks


def _order(**overrides: object) -> OrderRequest:
    defaults = dict(
        symbol="NIFTY24JUN23900PE",
        transaction_type=TransactionType.BUY,
        quantity=65,
        order_type=OrderType.MARKET,
        entry_price=165.0,
        stop_loss=150.0,
        target=198.0,
        signal_id=None,  # executor is signal-agnostic; the FK link is covered in test_execution_repository
    )
    defaults.update(overrides)
    return OrderRequest(**defaults)  # type: ignore[arg-type]


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
        kite_api_key=None,
        kite_api_secret=None,
        kite_access_token=None,
    )
    defaults.update(overrides)
    return Config(**defaults)  # type: ignore[arg-type]


# --- Valid order request ------------------------------------------------------


def test_valid_order_returns_success(executor: DryRunExecutor) -> None:
    result = executor.execute(_order())
    assert result.status is ExecutionStatus.SUCCESS
    assert result.succeeded is True
    assert result.remarks == "Order would have been submitted successfully."
    assert result.broker_order_id is None  # nothing was actually submitted
    assert result.timestamp == NOW


def test_valid_limit_order_with_price_succeeds(executor: DryRunExecutor) -> None:
    result = executor.execute(_order(order_type=OrderType.LIMIT, entry_price=150.0))
    assert result.status is ExecutionStatus.SUCCESS


def test_executor_mode_is_dry_run(executor: DryRunExecutor) -> None:
    assert executor.mode == "dry_run"
    assert isinstance(executor, Executor)


# --- Invalid order request ----------------------------------------------------


def test_invalid_order_is_rejected_not_submitted(executor: DryRunExecutor) -> None:
    result = executor.execute(_order(quantity=0))
    assert result.status is ExecutionStatus.REJECTED
    assert "quantity must be positive" in result.remarks


def test_limit_order_without_price_is_rejected(executor: DryRunExecutor) -> None:
    result = executor.execute(_order(order_type=OrderType.LIMIT, entry_price=None))
    assert result.status is ExecutionStatus.REJECTED
    assert "entry_price" in result.remarks


def test_missing_symbol_is_rejected(executor: DryRunExecutor) -> None:
    result = executor.execute(_order(symbol="  "))
    assert result.status is ExecutionStatus.REJECTED


# --- Logging ------------------------------------------------------------------


def test_dry_run_logs_what_would_be_submitted(
    executor: DryRunExecutor, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="teletrader.execution.dry_run"):
        executor.execute(_order(symbol="TCS", quantity=20, entry_price=3500.0, stop_loss=3450.0))
    log = caplog.text
    assert "[DRY RUN]" in log
    assert "BUY TCS" in log
    assert "Quantity: 20" in log
    assert "Order Type: MARKET" in log
    assert "Entry: 3500" in log
    assert "Stop Loss: 3450" in log
    assert "Order would have been submitted successfully." in log


# --- Execution history persistence --------------------------------------------


def test_successful_execution_is_persisted(
    executor: DryRunExecutor, repo: ExecutionRepository
) -> None:
    executor.execute(_order())
    stored = repo.list_all()
    assert len(stored) == 1
    assert stored[0].status is ExecutionStatus.SUCCESS
    assert stored[0].symbol == "NIFTY24JUN23900PE"


def test_rejected_execution_is_also_persisted(
    executor: DryRunExecutor, repo: ExecutionRepository
) -> None:
    executor.execute(_order(quantity=-5))
    stored = repo.list_all()
    assert len(stored) == 1
    assert stored[0].status is ExecutionStatus.REJECTED


# --- Configuration switching --------------------------------------------------


def test_factory_builds_dry_run_executor(repo: ExecutionRepository) -> None:
    ex = create_executor(_config(execution_mode="dry_run"), repo)
    assert isinstance(ex, DryRunExecutor)
    assert ex.mode == "dry_run"


def test_factory_builds_kite_executor(repo: ExecutionRepository) -> None:
    from teletrader.execution import KiteExecutor

    ex = create_executor(
        _config(
            execution_mode="kite",
            kite_api_key="k",
            kite_api_secret="s",
            kite_access_token="t",
        ),
        repo,
    )
    assert isinstance(ex, KiteExecutor)
    assert ex.mode == "kite"


def test_factory_builds_fyers_executor(repo: ExecutionRepository) -> None:
    from teletrader.execution import FyersExecutor

    ex = create_executor(
        _config(
            execution_mode="fyers",
            fyers_app_id="APP-100",
            fyers_secret_id="s",
            fyers_access_token="t",
        ),
        repo,
    )
    assert isinstance(ex, FyersExecutor)
    assert ex.mode == "fyers"


def test_factory_builds_fyers_shadow_executor(repo: ExecutionRepository) -> None:
    from teletrader.execution import FyersExecutor, FyersShadowExecutor

    ex = create_executor(
        _config(
            execution_mode="fyers_shadow",
            fyers_app_id="APP-100",
            fyers_secret_id="s",
            fyers_access_token="t",
        ),
        repo,
    )
    # It is the live executor's machinery, but selecting it must never produce
    # the live executor itself.
    assert isinstance(ex, FyersShadowExecutor)
    assert isinstance(ex, FyersExecutor)
    assert ex.mode == "fyers_shadow"


def test_shadow_mode_requires_fyers_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Shadow mode places no order but still calls FYERS (symbol master, funds),
    # so a missing daily token must fail at startup rather than mid-session.
    for var, value in (
        ("TELEGRAM_API_ID", "1"),
        ("TELEGRAM_API_HASH", "h"),
        ("TELEGRAM_PHONE", "+1"),
        ("TELEGRAM_CHANNEL", "me"),
        ("CHANNEL_2_BROKER", "fyers_shadow"),
    ):
        monkeypatch.setenv(var, value)
    with pytest.raises(ConfigError):
        Config.from_env()


def test_factory_mode_override_selects_per_channel_broker(repo: ExecutionRepository) -> None:
    from teletrader.execution import DryRunExecutor, KiteExecutor

    # Global default is dry_run, but the explicit mode arg (a channel's broker)
    # wins — this is how each channel builds its own executor.
    config = _config(
        execution_mode="dry_run",
        kite_api_key="k",
        kite_api_secret="s",
        kite_access_token="t",
    )
    assert isinstance(create_executor(config, repo), DryRunExecutor)
    assert isinstance(create_executor(config, repo, mode="kite"), KiteExecutor)


def test_broker_for_resolves_override_then_default() -> None:
    default = _config(execution_mode="dry_run")
    assert default.broker_for("channel1") == "dry_run"

    overridden = _config(execution_mode="dry_run", channel_1_broker="kite", channel_2_broker="fyers")
    assert overridden.broker_for("channel1") == "kite"
    assert overridden.broker_for("channel2") == "fyers"
    assert overridden.broker_for("channel3") == "dry_run"  # unknown -> global default


def test_channel_broker_requires_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    # A channel routed to fyers must have FYERS creds even if EXECUTION_MODE is dry_run.
    for var, value in (
        ("TELEGRAM_API_ID", "1"),
        ("TELEGRAM_API_HASH", "h"),
        ("TELEGRAM_PHONE", "+1"),
        ("TELEGRAM_CHANNEL", "me"),
        ("CHANNEL_1_BROKER", "fyers"),
    ):
        monkeypatch.setenv(var, value)
    with pytest.raises(ConfigError):
        Config.from_env()


def test_unknown_execution_mode_is_a_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An unrecognised EXECUTION_MODE is rejected at config load (fail-fast).
    for var, value in (
        ("TELEGRAM_API_ID", "1"),
        ("TELEGRAM_API_HASH", "h"),
        ("TELEGRAM_PHONE", "+1"),
        ("TELEGRAM_CHANNEL", "me"),
        ("EXECUTION_MODE", "nonsense"),
    ):
        monkeypatch.setenv(var, value)
    with pytest.raises(ConfigError):
        Config.from_env()


# --- Exception handling -------------------------------------------------------


def test_validate_order_raises_invalid_order_error() -> None:
    with pytest.raises(InvalidOrderError):
        validate_order(_order(quantity=0))


def test_invalid_order_error_is_an_execution_error() -> None:
    assert issubclass(InvalidOrderError, ExecutionError)
    with pytest.raises(ExecutionError):
        validate_order(_order(symbol=""))


def test_valid_order_passes_validation() -> None:
    # Should not raise.
    validate_order(_order())
