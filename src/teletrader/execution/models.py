"""Value objects exchanged across the execution boundary.

These are deliberately **broker-independent**: an :class:`OrderRequest` describes
*what to submit* in neutral terms, and an :class:`ExecutionResult` describes *what
happened to the attempt*. Neither knows about Telegram, the parser, or any broker
SDK — so the same objects flow through the :class:`~teletrader.execution.dry_run.DryRunExecutor`
today and the future ``KiteExecutor`` unchanged.

Every model is a frozen, slotted dataclass (per project convention): immutable
values passed between layers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

__all__ = [
    "ExecutionResult",
    "ExecutionStatus",
    "OrderRequest",
    "OrderType",
    "ProductType",
    "TransactionType",
]


class TransactionType(str, Enum):
    """Side of the order."""

    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    """How the order is priced (broker-neutral spellings; adapters translate)."""

    MARKET = "MARKET"
    LIMIT = "LIMIT"


class ProductType(str, Enum):
    """Margin product. ``INTRADAY`` (Kite ``MIS``), ``MARGIN`` (Kite ``NRML``)."""

    INTRADAY = "INTRADAY"
    MARGIN = "MARGIN"
    DELIVERY = "DELIVERY"


class ExecutionStatus(str, Enum):
    """Outcome of an execution attempt — describes the *attempt*, not a fill.

    ``SUCCESS`` means the order passed validation and would have been (dry run)
    or was (live) submitted to the broker. ``REJECTED`` means it failed
    validation and was never sent. ``FAILED`` means the broker call itself
    errored (used by the future live executor). The status is the same closed
    set for every executor.
    """

    SUCCESS = "SUCCESS"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """An instruction to submit a single order, in broker-neutral terms.

    ``entry_price`` doubles as the limit price for :attr:`OrderType.LIMIT`. The
    optional ``stop_loss``/``target`` travel with the request for logging and
    audit (and so a future bracket/SL order can use them). ``signal_id`` links
    the order back to the stored signal that produced it. This is a plain data
    carrier — validation lives in :mod:`teletrader.execution.validation`.
    """

    symbol: str
    transaction_type: TransactionType
    quantity: int
    order_type: OrderType = OrderType.MARKET
    product: ProductType = ProductType.INTRADAY
    exchange: str = "NFO"
    entry_price: float | None = None
    stop_loss: float | None = None
    target: float | None = None
    signal_id: int | None = None


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """The outcome of one execution attempt.

    ``order`` is the request that was attempted; ``status`` and ``remarks`` are
    the verdict (``remarks`` is a human-readable note suitable for logs and the
    execution history). ``broker_order_id`` is always ``None`` for a dry run and
    carries the broker's id once a live executor actually submits.
    """

    status: ExecutionStatus
    order: OrderRequest
    remarks: str
    broker_order_id: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def succeeded(self) -> bool:
        return self.status is ExecutionStatus.SUCCESS
