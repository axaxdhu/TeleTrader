"""The live Zerodha Kite Connect executor.

:class:`KiteExecutor` is the one place in the system that talks to a broker SDK.
It implements the same :class:`~teletrader.execution.base.Executor` interface as
:class:`~teletrader.execution.dry_run.DryRunExecutor`, so swapping
``EXECUTION_MODE`` from ``dry_run`` to ``kite`` is all it takes to go live — the
Trade Engine, pipeline, listener, and models are unchanged and remain completely
unaware of Kite.

Responsibilities, and *only* these:

* Build the Kite client at construction time from the configured credentials.
  Authentication is **manual**: a valid ``KITE_ACCESS_TOKEN`` is assumed to
  already exist. This module never automates login nor generates access tokens.
* Translate a broker-neutral :class:`OrderRequest` into Kite ``place_order``
  parameters (Kite-specific spellings live here and nowhere else).
* Place the order and return a broker-neutral :class:`ExecutionResult` — never a
  raw Kite response.
* Translate every Kite/transport failure into a structured ``ExecutionResult``
  (via the :mod:`~teletrader.execution.exceptions` hierarchy) instead of letting
  it crash the application.
* Log the attempt (timestamp, signal id, order, broker response, result,
  duration) and record it to SQLite — never logging secrets.

The Kite SDK is imported lazily (only when a real client is built) so that
``dry_run`` mode never pays the cost of loading it, and so this module is
importable without the SDK present (tests inject a fake client).
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
from .kite_instruments import (
    InstrumentResolver,
    KiteInstrumentResolver,
    ResolvedInstrument,
)
from .models import (
    ExecutionResult,
    ExecutionStatus,
    OrderRequest,
    OrderType,
    ProductType,
)
from .repository import ExecutionRepository
from .validation import validate_order

__all__ = ["KiteExecutor", "translate_broker_exception"]

logger = get_logger(__name__)

#: The mode string this executor implements (matches ``EXECUTION_MODE``).
MODE = "kite"

#: A clock returning the current (tz-aware) moment. Injectable for tests.
Clock = Callable[[], datetime]

#: Broker-neutral :class:`ProductType` → Kite product code.
_PRODUCT_CODES: dict[ProductType, str] = {
    ProductType.INTRADAY: "MIS",
    ProductType.MARGIN: "NRML",
    ProductType.DELIVERY: "CNC",
}

#: Kite "regular" order variety — the only one this app places.
_VARIETY_REGULAR = "regular"


class KiteClient(Protocol):
    """The slice of the Kite Connect client this executor depends on.

    Declaring it as a Protocol keeps the executor decoupled from the concrete
    ``kiteconnect.KiteConnect`` class and lets tests inject a fake. Covers both
    order placement and the instrument master used for symbol resolution.
    """

    def place_order(self, **params: Any) -> str:  # pragma: no cover - interface
        """Place an order and return the broker order id."""

    def instruments(self, exchange: str) -> list[dict[str, Any]]:  # pragma: no cover
        """Return the broker's instrument master for ``exchange``."""


class KiteExecutor(Executor):
    """Submits orders to Zerodha Kite Connect behind the :class:`Executor` seam.

    The Kite client, an execution repository (where attempts are recorded), and
    an optional clock are injected (DI). In production the client is built from
    the configured API key + access token; tests pass a fake ``client`` so no
    network call is ever made.
    """

    def __init__(
        self,
        repository: ExecutionRepository,
        *,
        api_key: str | None = None,
        access_token: str | None = None,
        client: KiteClient | None = None,
        resolver: InstrumentResolver | None = None,
        tz: tzinfo = timezone.utc,
        clock: Clock | None = None,
    ) -> None:
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._tz = tz
        self._kite: KiteClient = client if client is not None else _build_client(
            api_key, access_token
        )
        # Symbol resolution defaults to the Kite instrument master (same client);
        # tests inject a fake resolver so no instrument dump is ever fetched.
        self._resolver: InstrumentResolver = (
            resolver if resolver is not None else KiteInstrumentResolver(self._kite)
        )

    @property
    def mode(self) -> str:
        return MODE

    def execute(self, order: OrderRequest) -> ExecutionResult:
        """Place ``order`` on Kite and return a broker-neutral result.

        The order is validated by the shared gate first (so a request valid in a
        dry run is valid live), then its option is resolved to a concrete Kite
        tradingsymbol. A successful placement yields ``SUCCESS`` with the broker
        order id; a broker/instrument rejection yields ``REJECTED``; an auth /
        transport / unexpected failure yields ``FAILED``. Nothing is raised out of
        this method — every failure comes back as an :class:`ExecutionResult`.
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

        params = self._to_kite_params(order, instrument)
        try:
            broker_order_id = str(self._kite.place_order(**params))
        except Exception as exc:  # noqa: BLE001 — every failure becomes a result
            return self._fail(order, started, timestamp, exc, tradingsymbol=instrument.tradingsymbol)

        result = ExecutionResult(
            status=ExecutionStatus.SUCCESS,
            order=order,
            remarks=(
                f"Order submitted to Kite ({instrument.tradingsymbol}, "
                f"expiry {instrument.expiry.isoformat()})."
            ),
            broker_order_id=broker_order_id,
            timestamp=timestamp,
        )
        return self._finalize(
            result, started, broker_response=broker_order_id,
            tradingsymbol=instrument.tradingsymbol,
        )

    def _resolve(self, order: OrderRequest, *, on_date: Any) -> ResolvedInstrument:
        """Resolve the order's option to a concrete Kite contract.

        Requires the structured option fields the pipeline attaches; a missing one
        is a clean :class:`InstrumentNotFoundError` (→ ``REJECTED``) rather than a
        crash.
        """
        if order.underlying is None or order.strike is None or order.option_type is None:
            raise InstrumentNotFoundError(
                f"Order {order.symbol!r} is missing the option details "
                "(underlying/strike/option_type) needed to resolve a tradingsymbol"
            )
        return self._resolver.resolve(
            order.underlying, order.strike, order.option_type, on_date=on_date
        )

    def _to_kite_params(
        self, order: OrderRequest, instrument: ResolvedInstrument
    ) -> dict[str, Any]:
        """Translate an :class:`OrderRequest` + resolved contract into Kite params.

        This is the only Kite-specific mapping in the codebase: the resolved
        ``tradingsymbol``/``exchange``, product codes, the ``regular`` variety,
        and the limit ``price`` field. The neutral spellings for transaction type
        (BUY/SELL) and order type (MARKET/LIMIT) match Kite's own, so they pass
        through directly.
        """
        params: dict[str, Any] = {
            "variety": _VARIETY_REGULAR,
            "exchange": instrument.exchange,
            "tradingsymbol": instrument.tradingsymbol,
            "transaction_type": order.transaction_type.value,
            "quantity": order.quantity,
            "product": _PRODUCT_CODES[order.product],
            "order_type": order.order_type.value,
        }
        if order.order_type is OrderType.LIMIT:
            params["price"] = order.entry_price
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
        error = translate_broker_exception(exc)
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
            "[KITE] %s %s tradingsymbol=%s qty=%s type=%s signal_id=%s status=%s "
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


def translate_broker_exception(exc: Exception) -> ExecutionError:
    """Translate a Kite SDK / transport exception into the neutral hierarchy.

    Classification is by exception type *name* (and the Kite ``code``/message) so
    this module need not import the heavy Kite SDK to recognise its errors, and so
    unit tests can raise the real ``kiteconnect`` exceptions. Anything
    unrecognised becomes a :class:`BrokerCommunicationError` ("unexpected"), so no
    failure ever escapes uncategorised.
    """
    name = type(exc).__name__
    message = str(getattr(exc, "message", "") or exc) or name
    low = message.lower()
    code = getattr(exc, "code", None)

    if name == "TokenException":
        return AuthenticationError(f"Invalid or expired Kite access token: {message}")

    if name == "NetworkException":
        if code == 429 or "too many requests" in low or "rate limit" in low:
            return RateLimitError(f"Kite rate limit hit: {message}")
        return BrokerCommunicationError(f"Network error contacting Kite: {message}")

    if name == "OrderException":
        if "margin" in low or "insufficient" in low or "funds" in low:
            return InsufficientMarginError(f"Insufficient margin: {message}")
        if "market" in low and ("closed" in low or "not open" in low):
            return OrderRejectedError(f"Market closed: {message}")
        return OrderRejectedError(f"Order rejected by broker: {message}")

    if name == "InputException":
        return OrderRejectedError(f"Invalid order parameters (e.g. symbol): {message}")

    if name in ("PermissionException", "DataException", "GeneralException"):
        return BrokerCommunicationError(f"Kite error ({name}): {message}")

    # requests-level transport failures (Timeout, ConnectionError, …).
    if "timeout" in name.lower() or "connection" in name.lower():
        return BrokerCommunicationError(f"Network failure contacting Kite: {message}")

    return BrokerCommunicationError(f"Unexpected error placing Kite order ({name}): {message}")


def _build_client(api_key: str | None, access_token: str | None) -> KiteClient:
    """Construct a real Kite client from credentials (lazy SDK import).

    The access token is assumed already valid (manual auth); this neither logs in
    nor generates a token. Credentials are never logged.
    """
    if not api_key or not access_token:
        raise AuthenticationError(
            "Kite credentials missing: KITE_API_KEY and KITE_ACCESS_TOKEN are "
            "required for EXECUTION_MODE=kite"
        )
    from kiteconnect import KiteConnect  # lazy: only loaded for live trading

    client = KiteConnect(api_key=api_key)
    client.set_access_token(access_token)
    logger.info("Kite client initialised (access token assumed valid; no login automated).")
    return client
