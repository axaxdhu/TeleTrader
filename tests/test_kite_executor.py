"""Unit tests for the live :class:`KiteExecutor` — all with mocked Kite responses.

No network call is ever made: a fake Kite client and a fake symbol resolver are
injected. Covers the cases called out for this phase — successful order, rejected
order, invalid token, insufficient funds, market closed, network timeout, rate
limiting, and an unexpected exception — plus symbol resolution, the order-parameter
translation, SQLite persistence, logging, and the guarantee that the Trade Engine
never sees a Kite-specific class.
"""

from __future__ import annotations

import inspect
import logging
import sqlite3
import sys
from datetime import date, datetime, timezone

import pytest
import requests
from kiteconnect.exceptions import (
    InputException,
    NetworkException,
    OrderException,
    TokenException,
)

from teletrader.execution import (
    ExecutionRepository,
    ExecutionStatus,
    Executor,
    InstrumentNotFoundError,
    KiteExecutor,
    ManagementAction,
    OrderRequest,
    OrderStatus,
    OrderType,
    ProductType,
    ResolvedInstrument,
    TransactionType,
)
from teletrader.execution.kite import translate_broker_exception
from teletrader.database import connect, initialize

NOW = datetime(2026, 6, 29, 10, 0, tzinfo=timezone.utc)

RESOLVED = ResolvedInstrument(
    tradingsymbol="NIFTY2570323900PE",
    exchange="NFO",
    expiry=date(2026, 7, 3),
    lot_size=65,
)


# --- Fakes / builders ---------------------------------------------------------


class FakeKite:
    """A stand-in Kite client: returns an order id or raises a chosen error.

    Also records modify/cancel calls and can replay a fixed ``order_history`` so
    the management ops and fill/OCO status lookups can be exercised offline.
    """

    def __init__(
        self,
        *,
        order_id: str | None = None,
        error: Exception | None = None,
        history: list[dict[str, object]] | None = None,
        modify_error: Exception | None = None,
        cancel_error: Exception | None = None,
    ) -> None:
        self._order_id = order_id
        self._error = error
        self._history = history
        self._modify_error = modify_error
        self._cancel_error = cancel_error
        self.calls: list[dict[str, object]] = []
        self.modified: list[dict[str, object]] = []
        self.cancelled: list[dict[str, object]] = []

    def place_order(self, **params: object) -> str:
        self.calls.append(params)
        if self._error is not None:
            raise self._error
        assert self._order_id is not None
        return self._order_id

    def modify_order(self, **params: object) -> str:
        self.modified.append(params)
        if self._modify_error is not None:
            raise self._modify_error
        return str(params.get("order_id"))

    def cancel_order(self, **params: object) -> str:
        self.cancelled.append(params)
        if self._cancel_error is not None:
            raise self._cancel_error
        return str(params.get("order_id"))

    def order_history(self, order_id: str) -> list[dict[str, object]]:
        if self._history is None:
            return []
        return self._history


class FakeResolver:
    """Returns a fixed contract (or raises), recording the resolve arguments."""

    def __init__(self, *, instrument: ResolvedInstrument | None = RESOLVED,
                 error: Exception | None = None) -> None:
        self._instrument = instrument
        self._error = error
        self.calls: list[tuple[str, int, str, date]] = []

    def resolve(self, underlying: str, strike: int, option_type: str, *, on_date: date
                ) -> ResolvedInstrument:
        self.calls.append((underlying, strike, option_type, on_date))
        if self._error is not None:
            raise self._error
        assert self._instrument is not None
        return self._instrument


@pytest.fixture
def connection() -> sqlite3.Connection:
    conn = connect(":memory:")
    initialize(conn)
    yield conn
    conn.close()


@pytest.fixture
def repo(connection: sqlite3.Connection) -> ExecutionRepository:
    return ExecutionRepository(connection)


def _executor(
    repo: ExecutionRepository, client: FakeKite, resolver: FakeResolver | None = None
) -> KiteExecutor:
    return KiteExecutor(
        repo, client=client, resolver=resolver or FakeResolver(), clock=lambda: NOW
    )


def _order(**overrides: object) -> OrderRequest:
    defaults = dict(
        symbol="NIFTY 23900 PE",
        transaction_type=TransactionType.BUY,
        quantity=65,
        order_type=OrderType.MARKET,
        product=ProductType.INTRADAY,
        exchange="NFO",
        entry_price=165.0,
        stop_loss=150.0,
        target=198.0,
        signal_id=None,  # FK link to a stored signal is covered in test_execution_repository
        underlying="NIFTY",
        strike=23900,
        option_type="PE",
    )
    defaults.update(overrides)
    return OrderRequest(**defaults)  # type: ignore[arg-type]


# --- Successful order ---------------------------------------------------------


def test_successful_order_returns_success_with_broker_id(repo: ExecutionRepository) -> None:
    kite = FakeKite(order_id="240629000123456")
    result = _executor(repo, kite).execute(_order())

    assert result.status is ExecutionStatus.SUCCESS
    assert result.succeeded is True
    assert result.broker_order_id == "240629000123456"
    assert result.timestamp == NOW
    assert "Order submitted to Kite" in result.remarks
    assert RESOLVED.tradingsymbol in result.remarks  # the resolved contract


def test_executor_mode_is_kite(repo: ExecutionRepository) -> None:
    ex = _executor(repo, FakeKite(order_id="1"))
    assert ex.mode == "kite"
    assert isinstance(ex, Executor)


def test_order_request_is_translated_to_kite_params(repo: ExecutionRepository) -> None:
    kite = FakeKite(order_id="1")
    _executor(repo, kite).execute(_order())

    (params,) = kite.calls
    assert params == {
        "variety": "regular",
        "exchange": "NFO",
        "tradingsymbol": "NIFTY2570323900PE",  # the resolved tradingsymbol, not the display symbol
        "transaction_type": "BUY",
        "quantity": 65,
        "product": "MIS",  # INTRADAY -> MIS
        "order_type": "MARKET",
    }


def test_option_is_resolved_with_order_details(repo: ExecutionRepository) -> None:
    resolver = FakeResolver()
    _executor(repo, FakeKite(order_id="1"), resolver).execute(_order())

    # The structured option fields drive resolution; the date is the order date
    # in the market timezone (here equal to the UTC clock date).
    assert resolver.calls == [("NIFTY", 23900, "PE", date(2026, 6, 29))]


def test_market_timezone_determines_resolution_date(repo: ExecutionRepository) -> None:
    from zoneinfo import ZoneInfo

    resolver = FakeResolver()
    # 22:00 UTC on Jun 29 is 03:30 IST on Jun 30 — the trading day is the 30th.
    late = datetime(2026, 6, 29, 22, 0, tzinfo=timezone.utc)
    ex = KiteExecutor(
        repo, client=FakeKite(order_id="1"), resolver=resolver,
        tz=ZoneInfo("Asia/Kolkata"), clock=lambda: late,
    )
    ex.execute(_order())
    assert resolver.calls[0][3] == date(2026, 6, 30)


def test_unresolvable_option_is_rejected(repo: ExecutionRepository) -> None:
    resolver = FakeResolver(error=InstrumentNotFoundError("no contract"))
    kite = FakeKite(order_id="1")
    result = _executor(repo, kite, resolver).execute(_order())

    assert result.status is ExecutionStatus.REJECTED
    assert "no contract" in result.remarks
    assert kite.calls == []  # never tried to place the order


def test_missing_option_details_is_rejected(repo: ExecutionRepository) -> None:
    kite = FakeKite(order_id="1")
    result = _executor(repo, kite).execute(_order(underlying=None))

    assert result.status is ExecutionStatus.REJECTED
    assert "option details" in result.remarks
    assert kite.calls == []


def test_instrument_fetch_failure_is_a_failure(repo: ExecutionRepository) -> None:
    # If resolving needs the instrument master and that fetch fails (e.g. token),
    # it is a FAILED result, not a rejection.
    resolver = FakeResolver(error=TokenException("Invalid `access_token`."))
    result = _executor(repo, FakeKite(order_id="1"), resolver).execute(_order())

    assert result.status is ExecutionStatus.FAILED
    assert "access token" in result.remarks.lower()


def test_limit_order_includes_price(repo: ExecutionRepository) -> None:
    kite = FakeKite(order_id="1")
    _executor(repo, kite).execute(_order(order_type=OrderType.LIMIT, entry_price=150.0))

    (params,) = kite.calls
    assert params["order_type"] == "LIMIT"
    assert params["price"] == 150.0


def test_product_codes_map_to_kite(repo: ExecutionRepository) -> None:
    kite = FakeKite(order_id="1")
    ex = _executor(repo, kite)
    ex.execute(_order(product=ProductType.MARGIN))
    ex.execute(_order(product=ProductType.DELIVERY))
    assert kite.calls[0]["product"] == "NRML"
    assert kite.calls[1]["product"] == "CNC"


# --- Invalid order (shared validation gate, before any broker call) -----------


def test_invalid_order_is_rejected_without_calling_broker(repo: ExecutionRepository) -> None:
    kite = FakeKite(order_id="1")
    result = _executor(repo, kite).execute(_order(quantity=0))

    assert result.status is ExecutionStatus.REJECTED
    assert "quantity must be positive" in result.remarks
    assert kite.calls == []  # never reached the broker


# --- Rejected order -----------------------------------------------------------


def test_rejected_order_is_rejected(repo: ExecutionRepository) -> None:
    kite = FakeKite(error=OrderException("Order rejected by RMS"))
    result = _executor(repo, kite).execute(_order())

    assert result.status is ExecutionStatus.REJECTED
    assert "Order rejected by broker" in result.remarks
    assert result.broker_order_id is None


# --- Invalid access token -----------------------------------------------------


def test_invalid_access_token_fails(repo: ExecutionRepository) -> None:
    kite = FakeKite(error=TokenException("Invalid `access_token`."))
    result = _executor(repo, kite).execute(_order())

    assert result.status is ExecutionStatus.FAILED
    assert "access token" in result.remarks.lower()


# --- Insufficient funds -------------------------------------------------------


def test_insufficient_margin_is_rejected(repo: ExecutionRepository) -> None:
    kite = FakeKite(error=OrderException("Insufficient margin for this order"))
    result = _executor(repo, kite).execute(_order())

    assert result.status is ExecutionStatus.REJECTED
    assert "Insufficient margin" in result.remarks


# --- Market closed ------------------------------------------------------------


def test_market_closed_is_rejected(repo: ExecutionRepository) -> None:
    kite = FakeKite(error=OrderException("Markets are closed right now."))
    result = _executor(repo, kite).execute(_order())

    assert result.status is ExecutionStatus.REJECTED
    assert "Market closed" in result.remarks


# --- Invalid symbol -----------------------------------------------------------


def test_invalid_symbol_is_rejected(repo: ExecutionRepository) -> None:
    kite = FakeKite(error=InputException("Invalid `tradingsymbol`."))
    result = _executor(repo, kite).execute(_order())

    assert result.status is ExecutionStatus.REJECTED
    assert "symbol" in result.remarks.lower()


# --- Network failure / timeout ------------------------------------------------


def test_network_timeout_fails(repo: ExecutionRepository) -> None:
    kite = FakeKite(error=requests.exceptions.Timeout("read timed out"))
    result = _executor(repo, kite).execute(_order())

    assert result.status is ExecutionStatus.FAILED
    assert "network" in result.remarks.lower()


def test_network_exception_fails(repo: ExecutionRepository) -> None:
    kite = FakeKite(error=NetworkException("Gateway timeout", code=503))
    result = _executor(repo, kite).execute(_order())

    assert result.status is ExecutionStatus.FAILED
    assert "Network error contacting Kite" in result.remarks


# --- Rate limiting ------------------------------------------------------------


def test_rate_limit_fails(repo: ExecutionRepository) -> None:
    kite = FakeKite(error=NetworkException("Too many requests", code=429))
    result = _executor(repo, kite).execute(_order())

    assert result.status is ExecutionStatus.FAILED
    assert "rate limit" in result.remarks.lower()


# --- Unexpected exception -----------------------------------------------------


def test_unexpected_exception_fails_gracefully(repo: ExecutionRepository) -> None:
    kite = FakeKite(error=RuntimeError("boom"))
    result = _executor(repo, kite).execute(_order())

    assert result.status is ExecutionStatus.FAILED
    assert "Unexpected error" in result.remarks


# --- Persistence & logging ----------------------------------------------------


def test_every_attempt_is_persisted(repo: ExecutionRepository) -> None:
    ex_ok = _executor(repo, FakeKite(order_id="ID1"))
    ex_ok.execute(_order())
    _executor(repo, FakeKite(error=OrderException("rejected"))).execute(_order())

    stored = repo.list_all()
    assert len(stored) == 2
    assert stored[0].status is ExecutionStatus.SUCCESS
    assert stored[0].broker_order_id == "ID1"  # the broker order id is recorded
    assert stored[1].status is ExecutionStatus.REJECTED
    assert stored[1].broker_order_id is None


def test_attempt_is_logged_with_duration(
    repo: ExecutionRepository, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="teletrader.execution.kite"):
        _executor(repo, FakeKite(order_id="ID9")).execute(_order())
    log = caplog.text
    assert "[KITE]" in log
    assert "BUY NIFTY 23900 PE" in log  # readable display symbol
    assert "tradingsymbol=NIFTY2570323900PE" in log  # resolved contract
    assert "status=SUCCESS" in log
    assert "duration=" in log
    assert "broker_order_id=ID9" in log


def test_secrets_are_never_logged(
    repo: ExecutionRepository, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        _executor(repo, FakeKite(order_id="ID9")).execute(_order())
    # Credentials are not part of the order/result, so they can't appear in logs.
    assert "access_token" not in caplog.text.lower()


# --- Missing credentials (live build path) ------------------------------------


def test_missing_credentials_raises_authentication_error(repo: ExecutionRepository) -> None:
    from teletrader.execution.exceptions import AuthenticationError

    with pytest.raises(AuthenticationError):
        KiteExecutor(repo, api_key=None, access_token=None)


# --- Exception translation (unit) ---------------------------------------------


def test_translate_covers_each_category() -> None:
    from teletrader.execution.exceptions import (
        AuthenticationError,
        BrokerCommunicationError,
        InsufficientMarginError,
        OrderRejectedError,
        RateLimitError,
    )

    assert isinstance(translate_broker_exception(TokenException("x")), AuthenticationError)
    assert isinstance(
        translate_broker_exception(OrderException("low margin funds")),
        InsufficientMarginError,
    )
    assert isinstance(
        translate_broker_exception(OrderException("plain reject")), OrderRejectedError
    )
    assert isinstance(
        translate_broker_exception(NetworkException("Too many requests", code=429)),
        RateLimitError,
    )
    assert isinstance(
        translate_broker_exception(RuntimeError("?")), BrokerCommunicationError
    )


# --- The Trade Engine must never know about Kite ------------------------------


def test_trade_engine_has_no_kite_dependency() -> None:
    import teletrader.trade_engine as engine_module

    source = inspect.getsource(engine_module)
    assert "kite" not in source.lower()


# --- Protective SL-M order translation ----------------------------------------


def test_sl_m_order_includes_trigger_price(repo: ExecutionRepository) -> None:
    kite = FakeKite(order_id="SL1")
    sl_order = _order(
        transaction_type=TransactionType.SELL,
        order_type=OrderType.SL_M,
        entry_price=None,
        stop_loss=None,
        target=None,
        trigger_price=150.0,
    )
    result = _executor(repo, kite).execute(sl_order)

    assert result.status is ExecutionStatus.SUCCESS
    (params,) = kite.calls
    assert params["order_type"] == "SL-M"
    assert params["trigger_price"] == 150.0
    assert params["transaction_type"] == "SELL"
    assert "price" not in params


# --- Order state (fill detection + OCO) ---------------------------------------


def test_get_order_state_maps_complete_with_fill(repo: ExecutionRepository) -> None:
    kite = FakeKite(history=[{"status": "COMPLETE", "average_price": 165.5, "filled_quantity": 65}])
    state = _executor(repo, kite).get_order_state("OID")
    assert state.status is OrderStatus.COMPLETE
    assert state.is_filled
    assert state.average_price == 165.5
    assert state.filled_quantity == 65


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("OPEN", OrderStatus.PENDING),
        ("TRIGGER PENDING", OrderStatus.PENDING),
        ("REJECTED", OrderStatus.REJECTED),
        ("CANCELLED", OrderStatus.CANCELLED),
    ],
)
def test_get_order_state_maps_status(repo: ExecutionRepository, raw: str, expected) -> None:
    kite = FakeKite(history=[{"status": raw}])
    assert _executor(repo, kite).get_order_state("OID").status is expected


def test_get_order_state_unknown_for_missing_or_empty(repo: ExecutionRepository) -> None:
    ex = _executor(repo, FakeKite(history=[]))
    assert ex.get_order_state("OID").status is OrderStatus.UNKNOWN
    assert ex.get_order_state(None).status is OrderStatus.UNKNOWN


def test_get_order_state_never_raises(repo: ExecutionRepository) -> None:
    # A lookup failure degrades to UNKNOWN rather than crashing the caller.
    class Boom(FakeKite):
        def order_history(self, order_id: str) -> list[dict[str, object]]:
            raise NetworkException("down", code=503)

    assert _executor(repo, Boom()).get_order_state("OID").status is OrderStatus.UNKNOWN


# --- Cancel / modify (management ops) -----------------------------------------


def test_cancel_order_calls_kite(repo: ExecutionRepository) -> None:
    kite = FakeKite()
    result = _executor(repo, kite).cancel_order("OID", symbol="NIFTY 23900 PE")
    assert result.action is ManagementAction.CANCEL
    assert result.succeeded
    assert kite.cancelled == [{"variety": "regular", "order_id": "OID"}]


def test_cancel_order_without_id_fails(repo: ExecutionRepository) -> None:
    result = _executor(repo, FakeKite()).cancel_order(None)
    assert not result.succeeded
    assert "No broker order id" in result.remarks


def test_cancel_order_translates_broker_error(repo: ExecutionRepository) -> None:
    kite = FakeKite(cancel_error=OrderException("order is not open"))
    result = _executor(repo, kite).cancel_order("OID")
    assert not result.succeeded


def test_modify_stop_loss_calls_kite(repo: ExecutionRepository) -> None:
    kite = FakeKite()
    result = _executor(repo, kite).modify_stop_loss("SL1", 165.0)
    assert result.action is ManagementAction.MODIFY_STOP_LOSS
    assert result.succeeded
    assert kite.modified == [{"variety": "regular", "order_id": "SL1", "trigger_price": 165.0}]


def test_modify_target_calls_kite(repo: ExecutionRepository) -> None:
    kite = FakeKite()
    result = _executor(repo, kite).modify_target("TG1", 250.0)
    assert result.action is ManagementAction.MODIFY_TARGET
    assert result.succeeded
    assert kite.modified == [{"variety": "regular", "order_id": "TG1", "price": 250.0}]


def test_importing_decision_layer_does_not_load_kite_sdk() -> None:
    # Building the decision + execution layer must not drag in the heavy Kite SDK;
    # it is loaded lazily, only when a real KiteExecutor client is constructed.
    # Run in a clean subprocess so module-cache state can't mask the result.
    import subprocess

    code = (
        "import sys, teletrader.pipeline, teletrader.trade_engine, teletrader.execution;"
        "assert 'kiteconnect' not in sys.modules, sorted(m for m in sys.modules if 'kite' in m)"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
