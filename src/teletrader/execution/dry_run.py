"""The dry-run executor.

:class:`DryRunExecutor` validates an order and logs **exactly what would have
been submitted** to the broker, then records the attempt — but never contacts any
broker API. It exists to verify the whole execution pipeline end-to-end (engine →
executor → history) before live trading is switched on, and so it deliberately
does **not** simulate market movement, fills, positions, or P&L. The only
question it answers is: *would the correct order have been sent?*

It shares request validation and the persistence/result shapes with the future
``KiteExecutor``, so swapping ``EXECUTION_MODE`` from ``dry_run`` to ``kite``
changes the destination of the order, not the surrounding pipeline.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from ..logging_config import get_logger
from .base import Executor
from .exceptions import InvalidOrderError
from .models import ExecutionResult, ExecutionStatus, OrderRequest
from .repository import ExecutionRepository
from .validation import validate_order

__all__ = ["DryRunExecutor"]

logger = get_logger(__name__)

#: The mode string this executor implements (matches ``EXECUTION_MODE``).
MODE = "dry_run"

_SUCCESS_REMARK = "Order would have been submitted successfully."

#: A clock returning the current (tz-aware) moment. Injectable for tests.
Clock = Callable[[], datetime]


class DryRunExecutor(Executor):
    """Validates and logs orders without sending them to any broker.

    The execution repository (where attempts are recorded) and an optional clock
    are injected (DI), keeping the executor free of connection and time concerns.
    """

    def __init__(
        self,
        repository: ExecutionRepository,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def mode(self) -> str:
        return MODE

    def execute(self, order: OrderRequest) -> ExecutionResult:
        """Validate ``order``, log what would be sent, record it, return the result.

        A valid order yields a ``SUCCESS`` result (it *would* have been
        submitted); an invalid one yields a ``REJECTED`` result carrying the
        reason. Either way the attempt is logged and persisted — nothing is ever
        sent to a broker.
        """
        timestamp = self._clock()
        try:
            validate_order(order)
        except InvalidOrderError as exc:
            result = ExecutionResult(
                status=ExecutionStatus.REJECTED,
                order=order,
                remarks=f"Order rejected: {exc}",
                timestamp=timestamp,
            )
        else:
            result = ExecutionResult(
                status=ExecutionStatus.SUCCESS,
                order=order,
                remarks=_SUCCESS_REMARK,
                timestamp=timestamp,
            )

        self._log(result)
        self._repository.add(result)
        return result

    @staticmethod
    def _log(result: ExecutionResult) -> None:
        """Emit the human-readable ``[DRY RUN]`` block of the would-be order."""
        order = result.order
        logger.info(
            "[DRY RUN]\n"
            "%s %s\n"
            "Quantity: %s\n"
            "Order Type: %s\n"
            "Entry: %s\n"
            "Stop Loss: %s\n"
            "Target: %s\n"
            "Result:\n"
            "%s",
            order.transaction_type.value,
            order.symbol,
            order.quantity,
            order.order_type.value,
            _fmt(order.entry_price),
            _fmt(order.stop_loss),
            _fmt(order.target),
            result.remarks,
        )


def _fmt(value: float | None) -> str:
    """Render an optional price: ``-`` when absent, no trailing ``.0`` otherwise."""
    if value is None:
        return "-"
    return str(int(value)) if float(value).is_integer() else str(value)
