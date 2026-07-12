"""Order execution layer.

The broker-independent seam between the Trade Engine and a broker. The Trade
Engine decides; an :class:`Executor` submits the resulting order. Two
implementations are interchangeable behind the interface, chosen by
``EXECUTION_MODE``:

- :class:`DryRunExecutor` (``dry_run``) — validates, logs exactly what would be
  submitted, and records the attempt to SQLite. Sends nothing to any broker;
  models no fills, positions, or P&L. Used to validate the pipeline end-to-end.
- :class:`KiteExecutor` (``kite``) — submits live orders to Zerodha Kite behind
  this same interface.
- :class:`FyersExecutor` (``fyers``) — submits live orders to FYERS behind the
  same interface. Each channel can pick its own broker (``Config.broker_for``).

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
    AuthenticationError,
    BrokerCommunicationError,
    ExecutionError,
    InstrumentNotFoundError,
    InsufficientMarginError,
    InvalidOrderError,
    OrderRejectedError,
    RateLimitError,
)
from .factory import FYERS_MODE, KITE_MODE, create_executor
from .fyers import FyersExecutor
from .fyers_instruments import (
    FyersCsvSymbolMaster,
    FyersInstrumentResolver,
    FyersSymbolSource,
)
from .kite import KiteExecutor
from .kite_instruments import (
    InstrumentResolver,
    KiteInstrumentResolver,
    ResolvedInstrument,
)
from .models import (
    ExecutionResult,
    ExecutionStatus,
    ManagementAction,
    ManagementResult,
    OrderRequest,
    OrderState,
    OrderStatus,
    OrderType,
    ProductType,
    TransactionType,
)
from .repository import ExecutionRepository, StoredExecution
from .validation import validate_order

__all__ = [
    "AuthenticationError",
    "BrokerCommunicationError",
    "ExecutionError",
    "ExecutionRepository",
    "ExecutionResult",
    "ExecutionStatus",
    "Executor",
    "DryRunExecutor",
    "FYERS_MODE",
    "FyersCsvSymbolMaster",
    "FyersExecutor",
    "FyersInstrumentResolver",
    "FyersSymbolSource",
    "InstrumentNotFoundError",
    "InstrumentResolver",
    "InsufficientMarginError",
    "InvalidOrderError",
    "KITE_MODE",
    "KiteExecutor",
    "KiteInstrumentResolver",
    "ManagementAction",
    "ManagementResult",
    "OrderRejectedError",
    "OrderRequest",
    "OrderState",
    "OrderStatus",
    "OrderType",
    "ProductType",
    "RateLimitError",
    "ResolvedInstrument",
    "StoredExecution",
    "TransactionType",
    "create_executor",
    "validate_order",
]
