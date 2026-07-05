"""Applies trade-management commands to the active trade.

Where the :class:`~teletrader.pipeline.SignalPipeline` turns an *entry signal*
into an order, the :class:`TradeManager` turns a
:class:`~teletrader.commands.ManagementCommand` into the right action on an
*existing* trade:

* **AVOID** — cancel a not-yet-filled entry (``PENDING_ENTRY``).
* **BOOK_PROFIT** — exit an open position now, at market (a SELL order through the
  ordinary :meth:`Executor.execute`), then cancel its resting stop-loss.
* **MODIFY_STOP_LOSS / MODIFY_TARGET** — move the protective level (to *cost*, i.e.
  the entry price, or to an explicit number).

It is the seam between the parsed command and the broker-neutral
:class:`~teletrader.execution.base.Executor`, mirroring how
:func:`~teletrader.pipeline.build_order_request` bridges a ``Signal`` and an
``OrderRequest``. Correlation is **positional**: a bare command targets the most
recently active trade (:meth:`TradeRepository.most_recent_active`).

The actual broker order ids / fill detection are filled in by the live executor
(Phase 2); under the dry-run executor a trade is opened directly as ``OPEN`` with
the signal's levels, so book-profit and stop moves can be exercised end-to-end.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable

from .commands import CommandAction, ManagementCommand, PriceRef
from .execution import (
    ExecutionResult,
    Executor,
    OrderRequest,
    OrderStatus,
    OrderType,
    ProductType,
    TransactionType,
)
from .logging_config import get_logger
from .parser import Signal
from .trade_repository import StoredTrade, TradeRepository, TradeStatus

__all__ = ["ManagementOutcome", "ManagementReport", "TradeManager"]

logger = get_logger(__name__)

#: A clock returning the current (tz-aware) moment. Injectable for tests.
Clock = Callable[[], datetime]

#: Defaults for the bounded entry-fill poll (overridable for tests).
_FILL_TIMEOUT_SECONDS = 5.0
_FILL_POLL_INTERVAL = 0.5


class ManagementOutcome(str, Enum):
    """What became of a management command."""

    NO_TARGET = "no_target"            # no active trade to act on
    NOT_APPLICABLE = "not_applicable"  # command doesn't fit the trade's current state
    CANCELLED = "cancelled"            # a pending entry was cancelled (avoid)
    EXITED = "exited"                  # an open position was booked/exited
    MODIFIED = "modified"              # a stop-loss/target was moved
    FAILED = "failed"                  # the broker operation failed


@dataclass(frozen=True, slots=True)
class ManagementReport:
    """The result of applying one management command, for the caller to present."""

    outcome: ManagementOutcome
    command: ManagementCommand
    remarks: str
    trade: StoredTrade | None = None


class TradeManager:
    """Carries out management commands against the most recently active trade.

    Dependencies are injected (DI): the :class:`TradeRepository` (active-trade
    state) and the :class:`Executor` (broker operations — a dry run today). An
    optional clock keeps timestamps deterministic in tests.
    """

    def __init__(
        self,
        trades: TradeRepository,
        executor: Executor,
        *,
        clock: Clock | None = None,
        fill_timeout: float = _FILL_TIMEOUT_SECONDS,
        poll_interval: float = _FILL_POLL_INTERVAL,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._trades = trades
        self._executor = executor
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._fill_timeout = fill_timeout
        self._poll_interval = poll_interval
        self._sleep = sleep

    # --- Opening a position (entry → fill → protective orders) ----------------

    def open_position(
        self,
        signal: Signal,
        entry_order: OrderRequest,
        *,
        signal_id: int | None,
        when: datetime | None = None,
    ) -> ExecutionResult:
        """Submit the entry, await its fill, and place the protective orders.

        Returns the entry's :class:`ExecutionResult` (the pipeline presents it). On
        a successful, filled entry the position is recorded ``OPEN`` with a resting
        **SL-M** (stop) and **target LIMIT**, sized to the actual fill — so later
        management commands have real broker orders to act on. If the entry is
        accepted but not yet filled within the poll window, the trade is recorded
        ``PENDING_ENTRY`` (still cancellable via *Avoid*) and no protection is
        placed. A rejected/failed entry opens nothing.

        Under the dry-run executor this still runs end-to-end — the executor
        reports the order as filled and returns no broker ids, so the trade opens
        ``OPEN`` with ``NULL`` order handles.
        """
        result = self._executor.execute(entry_order)
        if not result.succeeded:
            return result  # entry rejected/failed — no position to track

        state = self._await_fill(result.broker_order_id)
        filled = state.status is OrderStatus.COMPLETE
        quantity = state.filled_quantity or entry_order.quantity
        entry_price = state.average_price if state.average_price is not None else signal.entry_price

        sl_id: str | None = None
        target_id: str | None = None
        if filled:
            sl_id = self._place_protective(
                signal, entry_order, quantity, signal_id, OrderType.SL_M
            )
            target_id = self._place_protective(
                signal, entry_order, quantity, signal_id, OrderType.LIMIT
            )
        else:
            logger.warning(
                "Entry %s not confirmed filled (%s); trade left PENDING, no protection placed",
                result.broker_order_id, state.raw,
            )

        self._trades.open_trade(
            signal_id=signal_id,
            underlying=signal.underlying,
            strike=signal.strike,
            option_type=signal.option_type,
            quantity=quantity,
            status=TradeStatus.OPEN if filled else TradeStatus.PENDING_ENTRY,
            entry_price=entry_price,
            stop_loss=signal.stop_loss,
            target=signal.target,
            entry_order_id=result.broker_order_id,
            sl_order_id=sl_id,
            target_order_id=target_id,
            created_at=when,
        )
        return result

    def _await_fill(self, broker_order_id: str | None):
        """Poll the entry order's state until terminal or the timeout elapses."""
        attempts = max(1, int(self._fill_timeout / self._poll_interval))
        state = self._executor.get_order_state(broker_order_id)
        for _ in range(attempts - 1):
            if state.status.is_terminal:
                break
            self._sleep(self._poll_interval)
            state = self._executor.get_order_state(broker_order_id)
        return state

    def _place_protective(
        self,
        signal: Signal,
        entry_order: OrderRequest,
        quantity: int,
        signal_id: int | None,
        order_type: OrderType,
    ) -> str | None:
        """Place one resting protective exit (SL-M or target LIMIT); return its id.

        A failure to place protection is logged loudly (the position is then
        unprotected on that leg) but does not abort — the entry is already live.
        """
        if order_type is OrderType.SL_M:
            if signal.stop_loss is None:
                return None
            order = self._protective_order(
                signal, entry_order, quantity, signal_id,
                order_type=OrderType.SL_M, trigger_price=signal.stop_loss,
            )
        else:  # target LIMIT (entry_price doubles as the limit price, per convention)
            if signal.target is None:
                return None
            order = self._protective_order(
                signal, entry_order, quantity, signal_id,
                order_type=OrderType.LIMIT, entry_price=signal.target,
            )
        result = self._executor.execute(order)
        if result.succeeded:
            return result.broker_order_id
        logger.error(
            "Protective %s placement FAILED for %s: %s",
            order_type.value, entry_order.symbol, result.remarks,
        )
        return None

    @staticmethod
    def _protective_order(
        signal: Signal,
        entry_order: OrderRequest,
        quantity: int,
        signal_id: int | None,
        *,
        order_type: OrderType,
        entry_price: float | None = None,
        trigger_price: float | None = None,
    ) -> OrderRequest:
        """A resting SELL exit (SL-M or LIMIT) flattening the long-option entry."""
        return OrderRequest(
            symbol=entry_order.symbol,
            transaction_type=TransactionType.SELL,
            quantity=quantity,
            order_type=order_type,
            product=entry_order.product,
            exchange=entry_order.exchange,
            entry_price=entry_price,
            trigger_price=trigger_price,
            signal_id=signal_id,
            underlying=signal.underlying,
            strike=signal.strike,
            option_type=signal.option_type.value,
        )

    # --- OCO reconciliation ---------------------------------------------------

    def reconcile(self) -> None:
        """Sync open trades with the broker: if a protective leg filled, cancel
        its sibling and close the trade.

        This is the app-managed one-cancels-other: the SL-M and target LIMIT are
        independent broker orders, so when one fills the other must be cancelled to
        avoid a naked exit. Called before handling each message so commands act on
        fresh state. Trades with no broker order ids (e.g. dry-run) are skipped.
        """
        for trade in self._trades.list_active():
            if trade.status is not TradeStatus.OPEN:
                continue
            if trade.sl_order_id is None and trade.target_order_id is None:
                continue

            if self._leg_filled(trade.sl_order_id):
                self._close_via(trade, hit="stop-loss", sibling=trade.target_order_id)
            elif self._leg_filled(trade.target_order_id):
                self._close_via(trade, hit="target", sibling=trade.sl_order_id)

    def _leg_filled(self, broker_order_id: str | None) -> bool:
        if broker_order_id is None:
            return False
        return self._executor.get_order_state(broker_order_id).status is OrderStatus.COMPLETE

    def _close_via(self, trade: StoredTrade, *, hit: str, sibling: str | None) -> None:
        if sibling is not None:
            self._executor.cancel_order(sibling, symbol=_symbol(trade))
        self._trades.update_status(trade.id, TradeStatus.CLOSED, when=self._clock())
        logger.info("Trade id=%s closed: %s hit (sibling cancelled)", trade.id, hit)

    def apply(self, command: ManagementCommand) -> ManagementReport:
        """Carry out ``command`` against the most recently active trade."""
        trade = self._trades.most_recent_active()
        if trade is None:
            return self._report(
                ManagementOutcome.NO_TARGET, command, "No active trade to act on."
            )

        if command.action is CommandAction.AVOID:
            return self._avoid(command, trade)
        if command.action is CommandAction.BOOK_PROFIT:
            return self._book_profit(command, trade)
        if command.action is CommandAction.MODIFY_STOP_LOSS:
            return self._move_stop_loss(command, trade)
        if command.action is CommandAction.MODIFY_TARGET:
            return self._move_target(command, trade)

        return self._report(  # defensive — every action is handled above
            ManagementOutcome.NOT_APPLICABLE, command,
            f"Unsupported command: {command.action.value}", trade,
        )

    # --- per-command handlers -------------------------------------------------

    def _avoid(self, command: ManagementCommand, trade: StoredTrade) -> ManagementReport:
        if trade.status is not TradeStatus.PENDING_ENTRY:
            return self._report(
                ManagementOutcome.NOT_APPLICABLE, command,
                "Trade already filled; cannot avoid (use book profit to exit).", trade,
            )
        result = self._executor.cancel_order(trade.entry_order_id, symbol=_symbol(trade))
        if not result.succeeded:
            return self._report(ManagementOutcome.FAILED, command, result.remarks, trade)
        self._trades.update_status(trade.id, TradeStatus.CANCELLED, when=self._clock())
        return self._report(
            ManagementOutcome.CANCELLED, command, "Pending entry cancelled.", trade
        )

    def _book_profit(
        self, command: ManagementCommand, trade: StoredTrade
    ) -> ManagementReport:
        if trade.status is not TradeStatus.OPEN:
            return self._report(
                ManagementOutcome.NOT_APPLICABLE, command,
                "No open position to book.", trade,
            )
        result = self._executor.execute(_exit_order(trade))
        if not result.succeeded:
            return self._report(ManagementOutcome.FAILED, command, result.remarks, trade)
        # The position is exited — drop the now-orphaned protective stop (best effort).
        if trade.sl_order_id is not None:
            self._executor.cancel_order(trade.sl_order_id, symbol=_symbol(trade))
        self._trades.update_status(trade.id, TradeStatus.CLOSED, when=self._clock())
        return self._report(
            ManagementOutcome.EXITED, command, "Position exited at market.", trade
        )

    def _move_stop_loss(
        self, command: ManagementCommand, trade: StoredTrade
    ) -> ManagementReport:
        if trade.status is not TradeStatus.OPEN:
            return self._report(
                ManagementOutcome.NOT_APPLICABLE, command,
                "No open position; nothing to protect.", trade,
            )
        new_sl = self._resolve_level(command, trade)
        if new_sl is None:
            return self._report(
                ManagementOutcome.NOT_APPLICABLE, command,
                "Cannot determine the new stop-loss (entry price unknown).", trade,
            )
        result = self._executor.modify_stop_loss(
            trade.sl_order_id, new_sl, symbol=_symbol(trade)
        )
        if not result.succeeded:
            return self._report(ManagementOutcome.FAILED, command, result.remarks, trade)
        self._trades.update_stop_loss(trade.id, new_sl, when=self._clock())
        return self._report(
            ManagementOutcome.MODIFIED, command, f"Stop-loss moved to {_fmt(new_sl)}.", trade
        )

    def _move_target(
        self, command: ManagementCommand, trade: StoredTrade
    ) -> ManagementReport:
        if trade.status is not TradeStatus.OPEN:
            return self._report(
                ManagementOutcome.NOT_APPLICABLE, command,
                "No open position; nothing to retarget.", trade,
            )
        new_target = self._resolve_level(command, trade)
        if new_target is None:
            return self._report(
                ManagementOutcome.NOT_APPLICABLE, command,
                "Cannot determine the new target.", trade,
            )
        result = self._executor.modify_target(
            trade.target_order_id, new_target, symbol=_symbol(trade)
        )
        if not result.succeeded:
            return self._report(ManagementOutcome.FAILED, command, result.remarks, trade)
        self._trades.update_target(trade.id, new_target, when=self._clock())
        return self._report(
            ManagementOutcome.MODIFIED, command, f"Target moved to {_fmt(new_target)}.", trade
        )

    @staticmethod
    def _resolve_level(command: ManagementCommand, trade: StoredTrade) -> float | None:
        """The numeric level a move-SL/target command asks for.

        ``to cost`` resolves to the trade's entry price (may be ``None`` if the
        fill price is not yet known); an explicit ``price`` is used as-is.
        """
        if command.to is PriceRef.COST:
            return trade.entry_price
        return command.price

    def _report(
        self,
        outcome: ManagementOutcome,
        command: ManagementCommand,
        remarks: str,
        trade: StoredTrade | None = None,
    ) -> ManagementReport:
        logger.info(
            "Command %s -> %s (trade=%s): %s",
            command, outcome.value, trade.id if trade else None, remarks,
        )
        return ManagementReport(outcome, command, remarks, trade)


def _symbol(trade: StoredTrade) -> str:
    """The human-readable instrument string for logs/exit orders."""
    return f"{trade.underlying} {trade.strike} {trade.option_type.value}"


def _exit_order(trade: StoredTrade) -> OrderRequest:
    """A market SELL to flatten the (long-option) position opened by the entry."""
    return OrderRequest(
        symbol=_symbol(trade),
        transaction_type=TransactionType.SELL,
        quantity=trade.quantity,
        order_type=OrderType.MARKET,
        product=ProductType.INTRADAY,
        exchange="NFO",
        signal_id=trade.signal_id,
        underlying=trade.underlying,
        strike=trade.strike,
        option_type=trade.option_type.value,
    )


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)
