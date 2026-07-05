"""The :class:`Executor` interface — the seam between the Trade Engine and a broker.

This is the abstraction the rest of the system depends on. The Trade Engine
decides *whether* to trade; an `Executor` is responsible for *submitting* the
resulting order — and the engine (and any caller) only ever sees this interface,
never a broker SDK. Two implementations are interchangeable behind it::

    Trade Engine
        │  OrderRequest
        ▼
    Executor  (this interface)
        ├── DryRunExecutor   — validates + logs + records; submits nothing
        └── KiteExecutor     — submits live orders to Zerodha Kite

Selecting which one runs is pure configuration (``EXECUTION_MODE``); see
:func:`teletrader.execution.factory.create_executor`. The interface is
deliberately small (one method): an executor *executes an order*. It does not
model positions, fills, or P&L — that is out of scope by design.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .models import (
    ExecutionResult,
    ExecutionStatus,
    ManagementAction,
    ManagementResult,
    OrderRequest,
    OrderState,
    OrderStatus,
)

__all__ = ["Executor"]


class Executor(ABC):
    """Submits orders on behalf of the Trade Engine.

    Implementations translate an :class:`OrderRequest` into whatever their target
    requires (a log line for the dry run; a Kite API call later) and return an
    :class:`ExecutionResult` describing the attempt.
    """

    @property
    @abstractmethod
    def mode(self) -> str:
        """The configured mode this executor implements (e.g. ``"dry_run"``)."""

    @abstractmethod
    def execute(self, order: OrderRequest) -> ExecutionResult:
        """Attempt to submit ``order`` and return the :class:`ExecutionResult`.

        Implementations validate the request, record the attempt, and either
        report success (``SUCCESS``) or rejection (``REJECTED``). A live executor
        may raise an :class:`~teletrader.execution.exceptions.ExecutionError`
        subclass if the broker call itself fails.
        """

    # --- Management operations on an existing order/position ------------------
    # These act on a trade that already exists (trade-management commands). They
    # are optional: the default raises nothing and reports ``FAILED`` ("not
    # supported in this mode") so an executor that has no live broker behind it
    # (or has not implemented them yet) degrades cleanly rather than crashing.
    # Booking profit is *not* here — it is an ordinary exit order via ``execute``.

    def cancel_order(
        self, broker_order_id: str | None, *, symbol: str | None = None
    ) -> ManagementResult:
        """Cancel a not-yet-filled order (e.g. to avoid a pending entry)."""
        return self._unsupported(ManagementAction.CANCEL)

    def modify_stop_loss(
        self, broker_order_id: str | None, new_trigger: float, *, symbol: str | None = None
    ) -> ManagementResult:
        """Move a resting stop-loss order's trigger to ``new_trigger``."""
        return self._unsupported(ManagementAction.MODIFY_STOP_LOSS)

    def modify_target(
        self, broker_order_id: str | None, new_price: float, *, symbol: str | None = None
    ) -> ManagementResult:
        """Move a resting target order's price to ``new_price``."""
        return self._unsupported(ManagementAction.MODIFY_TARGET)

    def get_order_state(self, broker_order_id: str | None) -> OrderState:
        """Return the live broker state of a placed order.

        Used for fill detection (did the entry reach ``COMPLETE``?) and OCO
        reconciliation (did a resting stop/target fill?). The default reports
        ``UNKNOWN`` — the safe non-terminal value, so an executor with no live
        broker behind it never causes a position to be treated as filled/closed.
        """
        return OrderState(OrderStatus.UNKNOWN, raw="not supported")

    def _unsupported(self, action: ManagementAction) -> ManagementResult:
        return ManagementResult(
            action=action,
            status=ExecutionStatus.FAILED,
            remarks=f"{action.value} is not supported in '{self.mode}' mode",
        )
