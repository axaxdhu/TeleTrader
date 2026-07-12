"""The live FYERS executor.

The FYERS counterpart to :class:`~teletrader.execution.kite.KiteExecutor`. It
implements the same :class:`~teletrader.execution.base.Executor` interface, so a
channel routed to the ``fyers`` broker trades through FYERS with no change to the
Trade Engine, pipeline, listener, or models — exactly as the Kite executor does
for Zerodha. It handles both **index** and **stock** options (the resolver picks
the right contract from the FYERS symbol master).

Responsibilities, and *only* these:

* Build the FYERS client at construction from the configured credentials.
  Authentication is **manual**: a valid daily ``FYERS_ACCESS_TOKEN`` is assumed to
  already exist. This module never logs in nor generates access tokens (see
  ``fyers_login.py``).
* Translate a broker-neutral :class:`OrderRequest` into FYERS ``place_order``
  parameters (FYERS's numeric codes live here and nowhere else).
* Place the order and return a broker-neutral :class:`ExecutionResult` — never a
  raw FYERS response.
* Translate every FYERS/transport failure into a structured result. Unlike Kite
  (whose SDK *raises* typed exceptions), FYERS reports business errors in the
  **response dict** (``{"s": "error", ...}``); both paths are handled.
* Log the attempt and record it to SQLite — never logging secrets.

The FYERS SDK is imported lazily (only when a real client is built) so this module
is importable without it, and other brokers/dry-run never pay its import cost.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone, tzinfo
from typing import Any, Callable, Protocol

from ..logging_config import get_logger
from .base import Executor
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
from .fyers_instruments import FyersCsvSymbolMaster, FyersInstrumentResolver
from .kite_instruments import InstrumentResolver, ResolvedInstrument
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
from .repository import ExecutionRepository
from .validation import validate_order

__all__ = ["FyersExecutor", "translate_fyers_exception"]

logger = get_logger(__name__)

#: The mode string this executor implements (matches a channel's broker setting).
MODE = "fyers"

#: A clock returning the current (tz-aware) moment. Injectable for tests.
Clock = Callable[[], datetime]

#: Broker-neutral :class:`ProductType` → FYERS product code.
_PRODUCT_CODES: dict[ProductType, str] = {
    ProductType.INTRADAY: "INTRADAY",
    ProductType.MARGIN: "MARGIN",
    ProductType.DELIVERY: "CNC",
}

#: Broker-neutral :class:`OrderType` → FYERS numeric order type.
#: 1 = Limit, 2 = Market, 3 = Stop (SL-M), 4 = Stop-limit.
_ORDER_TYPE_CODES: dict[OrderType, int] = {
    OrderType.LIMIT: 1,
    OrderType.MARKET: 2,
    OrderType.SL_M: 3,
}

#: Broker-neutral :class:`TransactionType` → FYERS numeric side (1 buy, -1 sell).
_SIDE_CODES: dict[TransactionType, int] = {
    TransactionType.BUY: 1,
    TransactionType.SELL: -1,
}

#: FYERS order-status integer → broker-neutral :class:`OrderStatus`.
#: 1 Cancelled, 2 Filled, 4 Transit, 5 Rejected, 6 Pending (3 is reserved).
_STATUS_MAP: dict[int, OrderStatus] = {
    1: OrderStatus.CANCELLED,
    2: OrderStatus.COMPLETE,
    4: OrderStatus.PENDING,
    5: OrderStatus.REJECTED,
    6: OrderStatus.PENDING,
}


class FyersClient(Protocol):
    """The slice of the FYERS SDK this executor depends on.

    A Protocol (not the concrete ``fyersModel.FyersModel``) so the executor stays
    decoupled from the SDK and tests can inject a fake. Every method returns the
    SDK's response dict.
    """

    def place_order(self, data: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover
        """Place an order; response carries the order ``id`` on success."""

    def modify_order(self, data: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover
        """Modify a working order (price/trigger/quantity)."""

    def cancel_order(self, data: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover
        """Cancel a working order."""

    def orderbook(self, data: dict[str, Any] | None = None) -> dict[str, Any]:  # pragma: no cover
        """Return the order book (optionally filtered to one ``id``)."""


class FyersExecutor(Executor):
    """Submits orders to FYERS behind the :class:`Executor` seam.

    The FYERS client, an execution repository (where attempts are recorded), and
    an optional clock are injected (DI). In production the client is built from the
    configured app id + access token; tests pass a fake ``client`` so no network
    call is ever made.
    """

    def __init__(
        self,
        repository: ExecutionRepository,
        *,
        app_id: str | None = None,
        access_token: str | None = None,
        client: FyersClient | None = None,
        resolver: InstrumentResolver | None = None,
        tz: tzinfo = timezone.utc,
        clock: Clock | None = None,
    ) -> None:
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._tz = tz
        self._fyers: FyersClient = client if client is not None else _build_client(
            app_id, access_token
        )
        # Symbol resolution defaults to the FYERS CSV symbol master; tests inject a
        # fake resolver so no master is ever downloaded.
        self._resolver: InstrumentResolver = (
            resolver if resolver is not None
            else FyersInstrumentResolver(FyersCsvSymbolMaster())
        )

    @property
    def mode(self) -> str:
        return MODE

    def execute(self, order: OrderRequest) -> ExecutionResult:
        """Place ``order`` on FYERS and return a broker-neutral result.

        Validated by the shared gate first, then the option is resolved to a
        concrete FYERS symbol. A successful placement yields ``SUCCESS`` with the
        broker order id; a business rejection (in the response or a typed error)
        yields ``REJECTED``; an auth/transport/unexpected failure yields
        ``FAILED``. Nothing is raised out of this method.
        """
        timestamp = self._clock()
        started = time.monotonic()

        try:
            validate_order(order)
        except InvalidOrderError as exc:
            return self._reject(order, started, timestamp, f"Order rejected: {exc}")

        try:
            instrument = self._resolve(order, on_date=timestamp.astimezone(self._tz).date())
        except InstrumentNotFoundError as exc:
            return self._reject(order, started, timestamp, str(exc))
        except Exception as exc:  # noqa: BLE001 — fetching the master failed
            return self._fail(order, started, timestamp, exc)

        params = self._to_fyers_params(order, instrument)
        try:
            response = self._fyers.place_order(data=params)
        except Exception as exc:  # noqa: BLE001 — every transport failure becomes a result
            return self._fail(order, started, timestamp, exc, tradingsymbol=instrument.tradingsymbol)

        broker_order_id, error = _interpret_order_response(response)
        if error is not None:
            result = ExecutionResult(
                status=_status_for(error),
                order=order,
                remarks=str(error),
                timestamp=timestamp,
            )
            return self._finalize(
                result, started, broker_response=str(response),
                tradingsymbol=instrument.tradingsymbol,
            )

        result = ExecutionResult(
            status=ExecutionStatus.SUCCESS,
            order=order,
            remarks=(
                f"Order submitted to FYERS ({instrument.tradingsymbol}, "
                f"expiry {instrument.expiry.isoformat()})."
            ),
            broker_order_id=broker_order_id,
            timestamp=timestamp,
        )
        return self._finalize(
            result, started, broker_response=str(response),
            tradingsymbol=instrument.tradingsymbol,
        )

    # --- Management operations (act on an order that already exists) -----------

    def get_order_state(self, broker_order_id: str | None) -> OrderState:
        """Look up a placed order's FYERS state (for fill detection + OCO).

        Never raises: a failed/empty lookup is reported as ``UNKNOWN`` so callers
        (the fill poll, OCO reconciliation) degrade safely rather than crash.
        """
        if not broker_order_id:
            return OrderState(OrderStatus.UNKNOWN, raw="no order id")
        try:
            response = self._fyers.orderbook(data={"id": broker_order_id})
        except Exception as exc:  # noqa: BLE001 — status checks must not crash callers
            logger.warning("[FYERS] orderbook(%s) failed: %r", broker_order_id, exc)
            return OrderState(OrderStatus.UNKNOWN, raw=repr(exc))
        order = _find_order(response, broker_order_id)
        if order is None:
            return OrderState(OrderStatus.UNKNOWN, raw="not in order book")
        raw_status = order.get("status")
        avg = order.get("tradedPrice")
        qty = order.get("filledQty")
        return OrderState(
            _STATUS_MAP.get(_as_int(raw_status), OrderStatus.PENDING),
            average_price=float(avg) if avg else None,
            filled_quantity=int(qty) if qty is not None else None,
            raw=str(raw_status),
        )

    def cancel_order(
        self, broker_order_id: str | None, *, symbol: str | None = None
    ) -> ManagementResult:
        if not broker_order_id:
            return self._mgmt_failed(ManagementAction.CANCEL, "No broker order id to cancel.")
        return self._mgmt_call(
            ManagementAction.CANCEL, broker_order_id, symbol,
            lambda: self._fyers.cancel_order(data={"id": broker_order_id}),
            f"Order {broker_order_id} cancelled.",
        )

    def modify_stop_loss(
        self, broker_order_id: str | None, new_trigger: float, *, symbol: str | None = None
    ) -> ManagementResult:
        if not broker_order_id:
            return self._mgmt_failed(
                ManagementAction.MODIFY_STOP_LOSS, "No stop-loss order to modify."
            )
        return self._mgmt_call(
            ManagementAction.MODIFY_STOP_LOSS, broker_order_id, symbol,
            lambda: self._fyers.modify_order(
                data={"id": broker_order_id, "type": _ORDER_TYPE_CODES[OrderType.SL_M],
                      "stopPrice": new_trigger}
            ),
            f"Stop-loss trigger moved to {new_trigger}.",
        )

    def modify_target(
        self, broker_order_id: str | None, new_price: float, *, symbol: str | None = None
    ) -> ManagementResult:
        if not broker_order_id:
            return self._mgmt_failed(
                ManagementAction.MODIFY_TARGET, "No target order to modify."
            )
        return self._mgmt_call(
            ManagementAction.MODIFY_TARGET, broker_order_id, symbol,
            lambda: self._fyers.modify_order(
                data={"id": broker_order_id, "type": _ORDER_TYPE_CODES[OrderType.LIMIT],
                      "limitPrice": new_price}
            ),
            f"Target price moved to {new_price}.",
        )

    def _mgmt_call(
        self,
        action: ManagementAction,
        broker_order_id: str,
        symbol: str | None,
        call: Callable[[], dict[str, Any]],
        success_remark: str,
    ) -> ManagementResult:
        """Run a FYERS cancel/modify call, translating any failure to a result.

        Both a raised transport error *and* a business error reported in the
        response dict become a ``FAILED``/``REJECTED`` :class:`ManagementResult`.
        """
        suffix = f" ({symbol})" if symbol else ""
        try:
            response = call()
        except Exception as exc:  # noqa: BLE001 — every transport failure becomes a result
            error = translate_fyers_exception(exc)
            logger.info(
                "[FYERS] %s order=%s%s -> FAILED: %s", action.value, broker_order_id, suffix, error
            )
            return ManagementResult(
                action, _status_for(error), str(error), broker_order_id, self._clock()
            )
        error = _error_from_response(response)
        if error is not None:
            logger.info(
                "[FYERS] %s order=%s%s -> FAILED: %s", action.value, broker_order_id, suffix, error
            )
            return ManagementResult(
                action, _status_for(error), str(error), broker_order_id, self._clock()
            )
        logger.info("[FYERS] %s order=%s%s -> SUCCESS", action.value, broker_order_id, suffix)
        return ManagementResult(
            action, ExecutionStatus.SUCCESS, success_remark, broker_order_id, self._clock()
        )

    def _mgmt_failed(self, action: ManagementAction, remarks: str) -> ManagementResult:
        return ManagementResult(action, ExecutionStatus.FAILED, remarks, None, self._clock())

    def _resolve(self, order: OrderRequest, *, on_date: Any) -> ResolvedInstrument:
        """Resolve the order's option to a concrete FYERS contract.

        Requires the structured option fields the pipeline attaches; a missing one
        is a clean :class:`InstrumentNotFoundError` (→ ``REJECTED``) rather than a
        crash.
        """
        if order.underlying is None or order.strike is None or order.option_type is None:
            raise InstrumentNotFoundError(
                f"Order {order.symbol!r} is missing the option details "
                "(underlying/strike/option_type) needed to resolve a FYERS symbol"
            )
        return self._resolver.resolve(
            order.underlying, order.strike, order.option_type, on_date=on_date
        )

    def _to_fyers_params(
        self, order: OrderRequest, instrument: ResolvedInstrument
    ) -> dict[str, Any]:
        """Translate an :class:`OrderRequest` + resolved contract into FYERS params.

        This is the only FYERS-specific mapping in the codebase: the resolved
        symbol string, the numeric side/type codes, the product code, and the
        limit/stop price fields.
        """
        params: dict[str, Any] = {
            "symbol": instrument.tradingsymbol,
            "qty": order.quantity,
            "type": _ORDER_TYPE_CODES[order.order_type],
            "side": _SIDE_CODES[order.transaction_type],
            "productType": _PRODUCT_CODES[order.product],
            "limitPrice": 0,
            "stopPrice": 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
        }
        if order.order_type is OrderType.LIMIT:
            params["limitPrice"] = order.entry_price
        elif order.order_type is OrderType.SL_M:
            params["stopPrice"] = order.trigger_price
        return params

    def _reject(
        self, order: OrderRequest, started: float, timestamp: datetime, remarks: str
    ) -> ExecutionResult:
        """Build, log, and persist a ``REJECTED`` result."""
        result = ExecutionResult(
            status=ExecutionStatus.REJECTED,
            order=order,
            remarks=remarks,
            timestamp=timestamp,
        )
        return self._finalize(result, started, broker_response=None)

    def _fail(
        self,
        order: OrderRequest,
        started: float,
        timestamp: datetime,
        exc: Exception,
        *,
        tradingsymbol: str | None = None,
    ) -> ExecutionResult:
        """Translate a broker/transport exception into a structured result."""
        error = translate_fyers_exception(exc)
        result = ExecutionResult(
            status=_status_for(error),
            order=order,
            remarks=str(error),
            timestamp=timestamp,
        )
        return self._finalize(
            result, started, broker_response=repr(exc), tradingsymbol=tradingsymbol
        )

    def _finalize(
        self,
        result: ExecutionResult,
        started: float,
        *,
        broker_response: str | None,
        tradingsymbol: str | None = None,
    ) -> ExecutionResult:
        """Log the attempt (with duration) and persist it; return the result."""
        duration_ms = (time.monotonic() - started) * 1000.0
        order = result.order
        logger.info(
            "[FYERS] %s %s symbol=%s qty=%s type=%s signal_id=%s status=%s "
            "broker_order_id=%s response=%s duration=%.1fms remarks=%s",
            order.transaction_type.value,
            order.symbol,
            tradingsymbol or "-",
            order.quantity,
            order.order_type.value,
            order.signal_id,
            result.status.value,
            result.broker_order_id,
            broker_response,
            duration_ms,
            result.remarks,
        )
        self._repository.add(result)
        return result


def _status_for(error: ExecutionError) -> ExecutionStatus:
    """Map a translated execution error to its result status.

    A broker *order* rejection (incl. insufficient margin) is ``REJECTED``;
    anything else (auth, transport, rate limit, unexpected) is ``FAILED``.
    """
    if isinstance(error, (OrderRejectedError, InvalidOrderError)):
        return ExecutionStatus.REJECTED
    return ExecutionStatus.FAILED


def _interpret_order_response(
    response: Any,
) -> tuple[str | None, ExecutionError | None]:
    """Interpret a FYERS ``place_order`` response dict.

    Returns ``(broker_order_id, None)`` on success, or ``(None, error)`` when the
    response reports a business error or lacks an order id.
    """
    error = _error_from_response(response)
    if error is not None:
        return None, error
    order_id = response.get("id") if isinstance(response, dict) else None
    if not order_id:
        return None, OrderRejectedError(
            f"FYERS accepted the request but returned no order id: {response}"
        )
    return str(order_id), None


def _error_from_response(response: Any) -> ExecutionError | None:
    """Classify a FYERS response dict as an error, or ``None`` if it is ``ok``.

    FYERS signals success with ``s == "ok"``; anything else carries a ``message``
    (and ``code``) describing the failure, which is mapped to the neutral hierarchy
    by message heuristics — the same idea as the Kite translation, adapted to
    FYERS's dict-based errors.
    """
    if not isinstance(response, dict):
        return BrokerCommunicationError(f"Unexpected FYERS response: {response!r}")
    if str(response.get("s", "")).lower() == "ok":
        return None
    message = str(response.get("message") or response.get("s") or response)
    low = message.lower()
    if "margin" in low or "insufficient" in low or "funds" in low:
        return InsufficientMarginError(f"Insufficient margin: {message}")
    if "market" in low and ("closed" in low or "not open" in low):
        return OrderRejectedError(f"Market closed: {message}")
    if "token" in low or "auth" in low or "unauthor" in low or "invalid app" in low:
        return AuthenticationError(f"Invalid or expired FYERS access token: {message}")
    if "rate limit" in low or "too many" in low:
        return RateLimitError(f"FYERS rate limit hit: {message}")
    return OrderRejectedError(f"Order rejected by FYERS: {message}")


def translate_fyers_exception(exc: Exception) -> ExecutionError:
    """Translate a raised FYERS/transport exception into the neutral hierarchy.

    FYERS reports most business errors in the response dict (see
    :func:`_error_from_response`); this handles the exceptions that *are* raised —
    transport failures (timeout/connection) and anything unexpected — so no failure
    ever escapes uncategorised.
    """
    name = type(exc).__name__
    message = str(getattr(exc, "message", "") or exc) or name
    low = message.lower()
    if "rate limit" in low or "too many" in low:
        return RateLimitError(f"FYERS rate limit hit: {message}")
    if "token" in low or "unauthor" in low:
        return AuthenticationError(f"FYERS authentication failed: {message}")
    if "timeout" in name.lower() or "timeout" in low or "connection" in name.lower() or "connection" in low:
        return BrokerCommunicationError(f"Network failure contacting FYERS: {message}")
    return BrokerCommunicationError(f"Unexpected error placing FYERS order ({name}): {message}")


def _find_order(response: Any, broker_order_id: str) -> dict[str, Any] | None:
    """Extract the matching order dict from a FYERS order-book response."""
    if not isinstance(response, dict):
        return None
    book = response.get("orderBook") or response.get("orderbook") or []
    if not isinstance(book, list):
        return None
    for order in book:
        if isinstance(order, dict) and str(order.get("id")) == str(broker_order_id):
            return order
    # A single-order query may return just the one order without an id echo.
    if len(book) == 1 and isinstance(book[0], dict):
        return book[0]
    return None


def _as_int(value: Any) -> int:
    """Coerce a FYERS status value to int; unknown → -1 (maps to PENDING)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _build_client(app_id: str | None, access_token: str | None) -> FyersClient:
    """Construct a real FYERS client from credentials (lazy SDK import).

    The access token is assumed already valid (manual auth); this neither logs in
    nor generates a token. Credentials are never logged.
    """
    if not app_id or not access_token:
        raise AuthenticationError(
            "FYERS credentials missing: FYERS_APP_ID and FYERS_ACCESS_TOKEN are "
            "required for the 'fyers' broker"
        )
    from fyers_apiv3 import fyersModel  # lazy: only loaded for live FYERS trading

    client = fyersModel.FyersModel(
        client_id=app_id, token=access_token, is_async=False, log_path=""
    )
    logger.info("FYERS client initialised (access token assumed valid; no login automated).")
    return client
