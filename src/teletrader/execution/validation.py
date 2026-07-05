"""Order-request validation, shared by every executor.

A single, broker-independent gate that an :class:`~teletrader.execution.models.OrderRequest`
must pass before it would be sent to a broker. Keeping it as one focused function
(rather than buried in an executor) means both the dry-run and the future live
executor validate identically, and the rules are trivially unit-testable.
"""

from __future__ import annotations

from .exceptions import InvalidOrderError
from .models import OrderRequest, OrderType

__all__ = ["validate_order"]


def validate_order(order: OrderRequest) -> None:
    """Raise :class:`InvalidOrderError` if ``order`` is not submittable.

    Checks the order is structurally sound: a symbol, a positive quantity, a
    price for limit orders, and non-negative prices where provided. Returns
    ``None`` when the order is valid.
    """
    if not order.symbol or not order.symbol.strip():
        raise InvalidOrderError("symbol is required")
    if order.quantity <= 0:
        raise InvalidOrderError(f"quantity must be positive, got {order.quantity}")
    if order.order_type is OrderType.LIMIT and (
        order.entry_price is None or order.entry_price <= 0
    ):
        raise InvalidOrderError("LIMIT order requires a positive entry_price")
    if order.order_type is OrderType.SL_M and (
        order.trigger_price is None or order.trigger_price <= 0
    ):
        raise InvalidOrderError("SL-M order requires a positive trigger_price")
    for name in ("entry_price", "stop_loss", "target", "trigger_price"):
        value = getattr(order, name)
        if value is not None and value <= 0:
            raise InvalidOrderError(f"{name} must be positive when set, got {value}")
