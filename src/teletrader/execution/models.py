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

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any

__all__ = [
    "ExecutionResult",
    "ShadowLeg",
    "ShadowReport",
    "ExecutionStatus",
    "ManagementAction",
    "ManagementResult",
    "OrderRequest",
    "OrderState",
    "OrderStatus",
    "OrderType",
    "ProductType",
    "TransactionType",
]


class TransactionType(str, Enum):
    """Side of the order."""

    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    """How the order is priced (broker-neutral spellings; adapters translate).

    ``SL_M`` is a stop-loss-market order: it rests until the market reaches
    ``trigger_price`` and then executes at market. It is the protective stop placed
    after an entry fills.
    """

    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL_M = "SL-M"


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

    ``symbol`` is a human-readable display string (e.g. ``"NIFTY 23900 PE"``) used
    for logs and the dry run. The structured ``underlying``/``strike``/
    ``option_type`` fields carry the same option in machine-readable form so a
    live executor can resolve the broker's exact tradingsymbol + expiry from them
    (the :class:`~teletrader.execution.dry_run.DryRunExecutor` ignores them).
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
    trigger_price: float | None = None  # the trigger for an SL-M order
    signal_id: int | None = None
    # Structured option details for broker symbol resolution (live executor).
    underlying: str | None = None
    strike: int | None = None
    option_type: str | None = None  # "CE" / "PE"


@dataclass(frozen=True, slots=True)
class ShadowLeg:
    """One protective order a shadow run built and withheld.

    A live entry is only half the plan: the stop-loss and target are placed as
    **separate** orders once the entry fills, and each can be refused on its own
    terms (a stop the wrong side of the price, a limit off the tick). Reporting
    them beside the entry is what makes "would this trade have worked?" a
    question about the whole position rather than just getting in.
    """

    kind: str  # "stop-loss" / "target"
    order_type: str
    price: float | None
    payload: Mapping[str, Any] | None = None
    accepted: bool = False
    note: str = ""


@dataclass(frozen=True, slots=True)
class ShadowReport:
    """What a *shadow* execution found on the way to the broker's door.

    Shadow mode answers a sharper question than a dry run: not "did we build a
    sane order?" but "would the broker have accepted this one?". Getting there
    means resolving the real contract and checking the account, so the findings —
    the concrete symbol, the exchange lot size, the quantity that follows from it,
    the exact payload that was about to be sent, and whether the account could
    have afforded it — are carried here for the alert the user reads on their
    phone.

    ``funds_ok`` is ``None`` when the balance could not be determined (the check
    was skipped or the call failed); that is reported honestly rather than being
    treated as a pass.
    """

    tradingsymbol: str
    exchange: str
    expiry: date
    lot_size: int
    lots: int
    quantity: int
    payload: Mapping[str, Any]
    funds_required: float | None = None
    funds_available: float | None = None
    funds_ok: bool | None = None
    funds_note: str = ""
    #: The resting protective exits that would follow the entry, in the order
    #: the live path places them (stop-loss, then target).
    protective: tuple[ShadowLeg, ...] = ()

    @property
    def fully_protected(self) -> bool:
        """Whether both protective legs would be accepted.

        A position whose stop would be refused is not a working trade, however
        cleanly the entry goes in.
        """
        return bool(self.protective) and all(leg.accepted for leg in self.protective)


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
    #: Set only by the shadow executor — the broker-ready order that was built
    #: and deliberately not sent. ``None`` for every other executor.
    shadow: ShadowReport | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is ExecutionStatus.SUCCESS


class ManagementAction(str, Enum):
    """A broker operation on an *existing* order/position (vs placing a new one).

    These back the trade-management commands: cancelling a not-yet-filled entry,
    or moving a resting stop-loss/target. (Booking profit is an ordinary exit
    order and uses :meth:`~teletrader.execution.base.Executor.execute`.)
    """

    CANCEL = "CANCEL"
    MODIFY_STOP_LOSS = "MODIFY_STOP_LOSS"
    MODIFY_TARGET = "MODIFY_TARGET"


@dataclass(frozen=True, slots=True)
class ManagementResult:
    """The outcome of a management operation (cancel / modify).

    Mirrors :class:`ExecutionResult` but carries no :class:`OrderRequest` — a
    cancel/modify acts on an order that already exists rather than submitting a
    new one. ``broker_order_id`` echoes the affected order when known (``None`` for
    a dry run, which touches no broker).
    """

    action: ManagementAction
    status: ExecutionStatus
    remarks: str
    broker_order_id: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def succeeded(self) -> bool:
        return self.status is ExecutionStatus.SUCCESS


class OrderStatus(str, Enum):
    """The live state of a placed order at the broker (broker-neutral).

    Drives two things: fill detection (has the entry reached ``COMPLETE``?) and
    OCO reconciliation (did a resting stop/target fill, so its sibling must be
    cancelled?). ``UNKNOWN`` covers "could not determine" — the safe non-terminal
    default so nothing is treated as filled by mistake.
    """

    PENDING = "PENDING"      # accepted/open/trigger-pending — not yet done
    COMPLETE = "COMPLETE"    # fully filled
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"

    @property
    def is_terminal(self) -> bool:
        """Whether the order has reached a final state (no longer working)."""
        return self in (OrderStatus.COMPLETE, OrderStatus.CANCELLED, OrderStatus.REJECTED)


@dataclass(frozen=True, slots=True)
class OrderState:
    """A snapshot of a placed order's broker state.

    ``average_price`` / ``filled_quantity`` are populated for a filled order (so a
    protective stop can be sized to the actual fill). ``raw`` keeps the broker's
    own status string for logs/debugging.
    """

    status: OrderStatus
    average_price: float | None = None
    filled_quantity: int | None = None
    raw: str = ""

    @property
    def is_filled(self) -> bool:
        return self.status is OrderStatus.COMPLETE
