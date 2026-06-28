"""Order execution layer.

The broker-independent seam between the Trade Engine and a broker. The Trade
Engine decides; an :class:`Executor` submits the resulting order. Two
implementations are interchangeable behind the interface, chosen by
``EXECUTION_MODE``:

- :class:`DryRunExecutor` (``dry_run``) — validates, logs exactly what would be
  submitted, and records the attempt to SQLite. Sends nothing to any broker;
  models no fills, positions, or P&L. Used to validate the pipeline end-to-end.
- ``KiteExecutor`` (``kite``) — submits to Zerodha Kite. Arrives next phase,
  behind this same interface.

This layer is independent of Telegram and of any broker SDK. Public surface
(import from ``teletrader.execution``):

- :class:`Executor`, :class:`DryRunExecutor`, :func:`create_executor`.
- Models: :class:`OrderRequest`, :class:`ExecutionResult`, and the enums
  :class:`ExecutionStatus`, :class:`OrderType`, :class:`ProductType`,
  :class:`TransactionType`.
- Persistence: :class:`ExecutionRepository`, :class:`StoredExecution`.
- Validation: :func:`validate_order`. Exceptions: :class:`ExecutionError` & co.
"""

from __future__ import annotations

from .base import Executor
from .dry_run import DryRunExecutor
from .exceptions import (
    BrokerCommunicationError,
    ExecutionError,
    InvalidOrderError,
    OrderRejectedError,
)
from .factory import KITE_MODE, create_executor
from .models import (
    ExecutionResult,
    ExecutionStatus,
    OrderRequest,
    OrderType,
    ProductType,
    TransactionType,
)
from .repository import ExecutionRepository, StoredExecution
from .validation import validate_order

__all__ = [
    "BrokerCommunicationError",
    "ExecutionError",
    "ExecutionRepository",
    "ExecutionResult",
    "ExecutionStatus",
    "Executor",
    "DryRunExecutor",
    "InvalidOrderError",
    "KITE_MODE",
    "OrderRejectedError",
    "OrderRequest",
    "OrderType",
    "ProductType",
    "StoredExecution",
    "TransactionType",
    "create_executor",
    "validate_order",
]
