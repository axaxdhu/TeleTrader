"""Unit tests for the trade manager (applies management commands to trades).

Uses real collaborators — an in-memory ``TradeRepository`` and a ``DryRunExecutor``
— so the command handlers, the executor's management operations, and the trade
state transitions are all exercised together. Trades are set up directly in the
repository at the status each case needs.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from teletrader.commands import CommandAction, ManagementCommand, PriceRef
from teletrader.database import connect, initialize
from teletrader.execution import (
    DryRunExecutor,
    ExecutionRepository,
    ExecutionResult,
    ExecutionStatus,
    Executor,
    ManagementAction,
    ManagementResult,
    OrderRequest,
    OrderState,
    OrderStatus,
    OrderType,
    ProductType,
    TransactionType,
)
from teletrader.parser import OptionType, parse_signal
from teletrader.trade_manager import ManagementOutcome, TradeManager
from teletrader.trade_repository import TradeRepository, TradeStatus

MOMENT = datetime(2026, 6, 30, 10, 0, tzinfo=timezone.utc)
SIGNAL = parse_signal("NIFTY 23900 PE ABOVE 165\n\nSL-150\n\nTGT-200+")  # SL 150, TGT 198


class FakeExecutor(Executor):
    """A controllable live-ish executor: assigns order ids, replays order states,
    and records placements / cancels / modifies — no broker, no network."""

    def __init__(self, *, states: dict[str, OrderState] | None = None) -> None:
        self._states = states or {}
        self._counter = 0
        self.placed: list[tuple[str, OrderRequest]] = []
        self.cancelled: list[str] = []
        self.modified: list[tuple[ManagementAction, str, float]] = []

    @property
    def mode(self) -> str:
        return "fake"

    def execute(self, order: OrderRequest) -> ExecutionResult:
        self._counter += 1
        oid = f"OID{self._counter}"
        self.placed.append((oid, order))
        return ExecutionResult(ExecutionStatus.SUCCESS, order, "placed", broker_order_id=oid)

    def get_order_state(self, broker_order_id: str | None) -> OrderState:
        if broker_order_id is None:
            return OrderState(OrderStatus.UNKNOWN)
        return self._states.get(broker_order_id, OrderState(OrderStatus.COMPLETE))

    def cancel_order(self, broker_order_id, *, symbol=None) -> ManagementResult:
        self.cancelled.append(broker_order_id)
        return ManagementResult(ManagementAction.CANCEL, ExecutionStatus.SUCCESS, "cancelled", broker_order_id)

    def modify_stop_loss(self, broker_order_id, new_trigger, *, symbol=None) -> ManagementResult:
        self.modified.append((ManagementAction.MODIFY_STOP_LOSS, broker_order_id, new_trigger))
        return ManagementResult(ManagementAction.MODIFY_STOP_LOSS, ExecutionStatus.SUCCESS, "ok", broker_order_id)

    def modify_target(self, broker_order_id, new_price, *, symbol=None) -> ManagementResult:
        self.modified.append((ManagementAction.MODIFY_TARGET, broker_order_id, new_price))
        return ManagementResult(ManagementAction.MODIFY_TARGET, ExecutionStatus.SUCCESS, "ok", broker_order_id)


def _entry_order() -> OrderRequest:
    return OrderRequest(
        symbol="NIFTY 23900 PE",
        transaction_type=TransactionType.BUY,
        quantity=65,
        order_type=OrderType.MARKET,
        product=ProductType.INTRADAY,
        exchange="NFO",
        entry_price=165.0,
        stop_loss=150.0,
        target=198.0,
        signal_id=None,
        underlying="NIFTY",
        strike=23900,
        option_type="PE",
    )


def _live_manager(trades: TradeRepository, executor: Executor) -> TradeManager:
    return TradeManager(
        trades, executor, clock=lambda: MOMENT,
        fill_timeout=0.05, poll_interval=0.01, sleep=lambda _s: None,
    )


@pytest.fixture
def connection() -> sqlite3.Connection:
    conn = connect(":memory:")
    initialize(conn)
    yield conn
    conn.close()


@pytest.fixture
def trades(connection: sqlite3.Connection) -> TradeRepository:
    return TradeRepository(connection, tz=timezone.utc)


@pytest.fixture
def executions(connection: sqlite3.Connection) -> ExecutionRepository:
    return ExecutionRepository(connection)


@pytest.fixture
def manager(
    trades: TradeRepository, executions: ExecutionRepository
) -> TradeManager:
    executor = DryRunExecutor(executions, clock=lambda: MOMENT)
    return TradeManager(trades, executor, clock=lambda: MOMENT)


def _open(trades: TradeRepository, *, status: TradeStatus, **overrides: object):
    defaults = dict(
        signal_id=None,
        underlying="NIFTY",
        strike=23900,
        option_type=OptionType.PUT,
        quantity=65,
        status=status,
        entry_price=165.0,
        stop_loss=150.0,
        target=198.0,
    )
    defaults.update(overrides)
    return trades.open_trade(**defaults)  # type: ignore[arg-type]


def _cmd(action: CommandAction, **kw: object) -> ManagementCommand:
    return ManagementCommand(action, raw_text=action.value, **kw)  # type: ignore[arg-type]


# --- No target ----------------------------------------------------------------

def test_no_active_trade_is_no_target(manager: TradeManager) -> None:
    report = manager.apply(_cmd(CommandAction.BOOK_PROFIT))
    assert report.outcome is ManagementOutcome.NO_TARGET
    assert report.trade is None


# --- Avoid --------------------------------------------------------------------

def test_avoid_cancels_pending_entry(
    manager: TradeManager, trades: TradeRepository
) -> None:
    trade = _open(trades, status=TradeStatus.PENDING_ENTRY)
    report = manager.apply(_cmd(CommandAction.AVOID))
    assert report.outcome is ManagementOutcome.CANCELLED
    assert trades.get(trade.id).status is TradeStatus.CANCELLED
    assert trades.most_recent_active() is None


def test_avoid_is_not_applicable_once_filled(
    manager: TradeManager, trades: TradeRepository
) -> None:
    trade = _open(trades, status=TradeStatus.OPEN)
    report = manager.apply(_cmd(CommandAction.AVOID))
    assert report.outcome is ManagementOutcome.NOT_APPLICABLE
    # The open trade is left untouched.
    assert trades.get(trade.id).status is TradeStatus.OPEN


# --- Book profit --------------------------------------------------------------

def test_book_profit_exits_open_position(
    manager: TradeManager, trades: TradeRepository, executions: ExecutionRepository
) -> None:
    trade = _open(trades, status=TradeStatus.OPEN)
    report = manager.apply(_cmd(CommandAction.BOOK_PROFIT))
    assert report.outcome is ManagementOutcome.EXITED
    assert trades.get(trade.id).status is TradeStatus.CLOSED
    # A market SELL for the full quantity was submitted (recorded).
    recorded = executions.list_all()
    assert len(recorded) == 1
    assert recorded[0].action is TransactionType.SELL
    assert recorded[0].quantity == 65


def test_book_profit_needs_an_open_position(
    manager: TradeManager, trades: TradeRepository, executions: ExecutionRepository
) -> None:
    _open(trades, status=TradeStatus.PENDING_ENTRY)
    report = manager.apply(_cmd(CommandAction.BOOK_PROFIT))
    assert report.outcome is ManagementOutcome.NOT_APPLICABLE
    assert executions.count() == 0  # nothing was exited


# --- Move stop-loss -----------------------------------------------------------

def test_move_sl_to_cost(manager: TradeManager, trades: TradeRepository) -> None:
    trade = _open(trades, status=TradeStatus.OPEN, entry_price=165.0, stop_loss=150.0)
    report = manager.apply(_cmd(CommandAction.MODIFY_STOP_LOSS, to=PriceRef.COST))
    assert report.outcome is ManagementOutcome.MODIFIED
    assert trades.get(trade.id).stop_loss == 165.0


def test_move_sl_to_explicit_price(
    manager: TradeManager, trades: TradeRepository
) -> None:
    trade = _open(trades, status=TradeStatus.OPEN)
    report = manager.apply(_cmd(CommandAction.MODIFY_STOP_LOSS, price=158.0))
    assert report.outcome is ManagementOutcome.MODIFIED
    assert trades.get(trade.id).stop_loss == 158.0


def test_move_sl_needs_open_position(
    manager: TradeManager, trades: TradeRepository
) -> None:
    _open(trades, status=TradeStatus.PENDING_ENTRY)
    report = manager.apply(_cmd(CommandAction.MODIFY_STOP_LOSS, to=PriceRef.COST))
    assert report.outcome is ManagementOutcome.NOT_APPLICABLE


def test_move_sl_to_cost_without_known_entry_is_not_applicable(
    manager: TradeManager, trades: TradeRepository
) -> None:
    trade = _open(trades, status=TradeStatus.OPEN, entry_price=None)
    report = manager.apply(_cmd(CommandAction.MODIFY_STOP_LOSS, to=PriceRef.COST))
    assert report.outcome is ManagementOutcome.NOT_APPLICABLE
    assert trades.get(trade.id).stop_loss == 150.0  # unchanged


# --- Move target --------------------------------------------------------------

def test_move_target_to_price(manager: TradeManager, trades: TradeRepository) -> None:
    trade = _open(trades, status=TradeStatus.OPEN, target=198.0)
    report = manager.apply(_cmd(CommandAction.MODIFY_TARGET, price=250.0))
    assert report.outcome is ManagementOutcome.MODIFIED
    assert trades.get(trade.id).target == 250.0


# --- Correlation: most-recent active ------------------------------------------

def test_command_targets_most_recent_active_trade(
    manager: TradeManager, trades: TradeRepository
) -> None:
    first = _open(trades, status=TradeStatus.OPEN, strike=23900)
    second = _open(trades, status=TradeStatus.OPEN, strike=24000)
    manager.apply(_cmd(CommandAction.MODIFY_STOP_LOSS, price=170.0))
    # Only the newest active trade is affected.
    assert trades.get(second.id).stop_loss == 170.0
    assert trades.get(first.id).stop_loss == 150.0


# --- open_position: entry -> fill -> protective orders ------------------------

def test_open_position_filled_places_protective_orders(
    trades: TradeRepository,
) -> None:
    executor = FakeExecutor(
        states={"OID1": OrderState(OrderStatus.COMPLETE, average_price=164.5, filled_quantity=65)}
    )
    _live_manager(trades, executor).open_position(SIGNAL, _entry_order(), signal_id=None)

    # Three orders placed: entry (BUY MARKET), then SL-M + target LIMIT (both SELL).
    assert len(executor.placed) == 3
    _, entry = executor.placed[0]
    _, sl = executor.placed[1]
    _, target = executor.placed[2]
    assert entry.transaction_type is TransactionType.BUY
    assert sl.order_type is OrderType.SL_M and sl.trigger_price == 150.0
    assert sl.transaction_type is TransactionType.SELL
    assert target.order_type is OrderType.LIMIT and target.entry_price == 198.0

    trade = trades.most_recent_active()
    assert trade.status is TradeStatus.OPEN
    assert trade.entry_price == 164.5            # the actual fill price
    assert trade.entry_order_id == "OID1"
    assert trade.sl_order_id == "OID2"
    assert trade.target_order_id == "OID3"


def test_open_position_unfilled_stays_pending_without_protection(
    trades: TradeRepository,
) -> None:
    executor = FakeExecutor(states={"OID1": OrderState(OrderStatus.PENDING)})
    _live_manager(trades, executor).open_position(SIGNAL, _entry_order(), signal_id=None)

    # Only the entry was placed; no protective orders for an unfilled entry.
    assert len(executor.placed) == 1
    trade = trades.most_recent_active()
    assert trade.status is TradeStatus.PENDING_ENTRY
    assert trade.entry_order_id == "OID1"
    assert trade.sl_order_id is None and trade.target_order_id is None


def test_open_position_rejected_entry_opens_no_trade(trades: TradeRepository) -> None:
    class Rejecting(FakeExecutor):
        def execute(self, order: OrderRequest) -> ExecutionResult:
            return ExecutionResult(ExecutionStatus.REJECTED, order, "rejected by RMS")

    result = _live_manager(trades, Rejecting()).open_position(
        SIGNAL, _entry_order(), signal_id=None
    )
    assert result.status is ExecutionStatus.REJECTED
    assert trades.most_recent_active() is None


def test_avoid_cancels_pending_entry_live(trades: TradeRepository) -> None:
    executor = FakeExecutor(states={"OID1": OrderState(OrderStatus.PENDING)})
    manager = _live_manager(trades, executor)
    manager.open_position(SIGNAL, _entry_order(), signal_id=None)

    report = manager.apply(_cmd(CommandAction.AVOID))
    assert report.outcome is ManagementOutcome.CANCELLED
    assert executor.cancelled == ["OID1"]  # the pending entry order
    assert trades.most_recent_active() is None


# --- OCO reconciliation -------------------------------------------------------

def test_reconcile_closes_and_cancels_sibling_when_stop_hits(
    trades: TradeRepository,
) -> None:
    trade = _open(
        trades, status=TradeStatus.OPEN, sl_order_id="SL1", target_order_id="TG1"
    )
    executor = FakeExecutor(states={
        "SL1": OrderState(OrderStatus.COMPLETE),  # stop hit
        "TG1": OrderState(OrderStatus.PENDING),
    })
    _live_manager(trades, executor).reconcile()

    assert executor.cancelled == ["TG1"]                       # sibling target cancelled
    assert trades.get(trade.id).status is TradeStatus.CLOSED


def test_reconcile_closes_and_cancels_sibling_when_target_hits(
    trades: TradeRepository,
) -> None:
    trade = _open(
        trades, status=TradeStatus.OPEN, sl_order_id="SL1", target_order_id="TG1"
    )
    executor = FakeExecutor(states={
        "SL1": OrderState(OrderStatus.PENDING),
        "TG1": OrderState(OrderStatus.COMPLETE),  # target hit
    })
    _live_manager(trades, executor).reconcile()

    assert executor.cancelled == ["SL1"]
    assert trades.get(trade.id).status is TradeStatus.CLOSED


def test_reconcile_leaves_trade_open_while_both_legs_pending(
    trades: TradeRepository,
) -> None:
    trade = _open(
        trades, status=TradeStatus.OPEN, sl_order_id="SL1", target_order_id="TG1"
    )
    executor = FakeExecutor(states={
        "SL1": OrderState(OrderStatus.PENDING),
        "TG1": OrderState(OrderStatus.PENDING),
    })
    _live_manager(trades, executor).reconcile()

    assert executor.cancelled == []
    assert trades.get(trade.id).status is TradeStatus.OPEN


def test_reconcile_skips_trades_without_broker_ids(trades: TradeRepository) -> None:
    # Dry-run trades carry no order ids — reconcile must not touch them.
    trade = _open(trades, status=TradeStatus.OPEN)  # sl/target ids None
    executor = FakeExecutor()  # get_order_state would say COMPLETE for any id
    _live_manager(trades, executor).reconcile()

    assert executor.cancelled == []
    assert trades.get(trade.id).status is TradeStatus.OPEN
