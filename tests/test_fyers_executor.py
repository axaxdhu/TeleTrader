"""Unit tests for the live :class:`FyersExecutor` — all with mocked FYERS responses.

No network call is ever made: a fake FYERS client and a fake symbol resolver are
injected. FYERS reports business errors in the **response dict** (unlike Kite,
which raises), so the tests exercise both response-dict errors and raised
transport errors. Covers the parity cases (success, rejection, margin, market
closed, token, timeout, rate limit, unexpected), plus resolution, the numeric
param translation, SL-M, order-state mapping, cancel/modify, persistence, logging,
and the guarantee that the Trade Engine never sees a FYERS-specific class.
"""

from __future__ import annotations

import inspect
import logging
import sqlite3
import sys
from datetime import date, datetime, timezone

import pytest

from teletrader.database import connect, initialize
from teletrader.execution import (
    ExecutionRepository,
    ExecutionStatus,
    Executor,
    FyersExecutor,
    InstrumentNotFoundError,
    ManagementAction,
    OrderRequest,
    OrderStatus,
    OrderType,
    ProductType,
    ResolvedInstrument,
    TransactionType,
)
from teletrader.execution.fyers import (
    _error_from_response,
    translate_fyers_exception,
)

NOW = datetime(2026, 6, 29, 10, 0, tzinfo=timezone.utc)

RESOLVED = ResolvedInstrument(
    tradingsymbol="NSE:NIFTY2570323900PE",
    exchange="NSE",
    expiry=date(2026, 7, 3),
    lot_size=75,
)


# --- Fakes / builders ---------------------------------------------------------


def _ok(order_id: str) -> dict[str, object]:
    return {"s": "ok", "code": 1101, "message": "order placed", "id": order_id}


def _err(message: str) -> dict[str, object]:
    return {"s": "error", "code": -99, "message": message}


class FakeFyers:
    """A stand-in FYERS client: returns a response dict or raises a chosen error.

    Records place/modify/cancel calls and can replay a fixed order book so the
    management ops and fill/OCO status lookups can be exercised offline.
    """

    def __init__(
        self,
        *,
        response: dict[str, object] | None = None,
        error: Exception | None = None,
        book: list[dict[str, object]] | None = None,
        modify_response: dict[str, object] | None = None,
        cancel_response: dict[str, object] | None = None,
    ) -> None:
        self._response = response
        self._error = error
        self._book = book
        self._modify_response = modify_response
        self._cancel_response = cancel_response
        self.calls: list[dict[str, object]] = []
        self.modified: list[dict[str, object]] = []
        self.cancelled: list[dict[str, object]] = []

    def place_order(self, data: dict[str, object]) -> dict[str, object]:
        self.calls.append(data)
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response

    def modify_order(self, data: dict[str, object]) -> dict[str, object]:
        self.modified.append(data)
        return self._modify_response or {"s": "ok", "message": "modified"}

    def cancel_order(self, data: dict[str, object]) -> dict[str, object]:
        self.cancelled.append(data)
        return self._cancel_response or {"s": "ok", "message": "cancelled"}

    def orderbook(self, data: dict[str, object] | None = None) -> dict[str, object]:
        return {"s": "ok", "orderBook": self._book or []}


class FakeResolver:
    """Returns a fixed contract (or raises), recording the resolve arguments."""

    def __init__(
        self, *, instrument: ResolvedInstrument | None = RESOLVED, error: Exception | None = None
    ) -> None:
        self._instrument = instrument
        self._error = error
        self.calls: list[tuple[str, int, str, date]] = []

    def resolve(
        self, underlying: str, strike: int, option_type: str, *, on_date: date
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
    repo: ExecutionRepository, client: FakeFyers, resolver: FakeResolver | None = None
) -> FyersExecutor:
    return FyersExecutor(
        repo, client=client, resolver=resolver or FakeResolver(), clock=lambda: NOW
    )


def _order(**overrides: object) -> OrderRequest:
    defaults = dict(
        symbol="NIFTY 23900 PE",
        transaction_type=TransactionType.BUY,
        quantity=75,
        order_type=OrderType.MARKET,
        product=ProductType.INTRADAY,
        exchange="NFO",
        entry_price=165.0,
        stop_loss=150.0,
        target=198.0,
        signal_id=None,
        underlying="NIFTY",
        strike=23900,
        option_type="PE",
    )
    defaults.update(overrides)
    return OrderRequest(**defaults)  # type: ignore[arg-type]


# --- Successful order ---------------------------------------------------------


def test_successful_order_returns_success_with_broker_id(repo: ExecutionRepository) -> None:
    fyers = FakeFyers(response=_ok("2506291234"))
    result = _executor(repo, fyers).execute(_order())

    assert result.status is ExecutionStatus.SUCCESS
    assert result.succeeded is True
    assert result.broker_order_id == "2506291234"
    assert result.timestamp == NOW
    assert "Order submitted to FYERS" in result.remarks
    assert RESOLVED.tradingsymbol in result.remarks


def test_executor_mode_is_fyers(repo: ExecutionRepository) -> None:
    ex = _executor(repo, FakeFyers(response=_ok("1")))
    assert ex.mode == "fyers"
    assert isinstance(ex, Executor)


def test_order_request_is_translated_to_fyers_params(repo: ExecutionRepository) -> None:
    fyers = FakeFyers(response=_ok("1"))
    _executor(repo, fyers).execute(_order())

    (params,) = fyers.calls
    assert params == {
        "symbol": "NSE:NIFTY2570323900PE",  # resolved symbol, not the display string
        "qty": 75,
        "type": 2,   # MARKET
        "side": 1,   # BUY
        "productType": "INTRADAY",
        "limitPrice": 0,
        "stopPrice": 0,
        "validity": "DAY",
        "disclosedQty": 0,
        "offlineOrder": False,
    }


def test_option_is_resolved_with_order_details(repo: ExecutionRepository) -> None:
    resolver = FakeResolver()
    _executor(repo, FakeFyers(response=_ok("1")), resolver).execute(_order())
    assert resolver.calls == [("NIFTY", 23900, "PE", date(2026, 6, 29))]


def test_market_timezone_determines_resolution_date(repo: ExecutionRepository) -> None:
    from zoneinfo import ZoneInfo

    resolver = FakeResolver()
    late = datetime(2026, 6, 29, 22, 0, tzinfo=timezone.utc)  # 03:30 IST next day
    ex = FyersExecutor(
        repo, client=FakeFyers(response=_ok("1")), resolver=resolver,
        tz=ZoneInfo("Asia/Kolkata"), clock=lambda: late,
    )
    ex.execute(_order())
    assert resolver.calls[0][3] == date(2026, 6, 30)


def test_unresolvable_option_is_rejected(repo: ExecutionRepository) -> None:
    resolver = FakeResolver(error=InstrumentNotFoundError("no contract"))
    fyers = FakeFyers(response=_ok("1"))
    result = _executor(repo, fyers, resolver).execute(_order())

    assert result.status is ExecutionStatus.REJECTED
    assert "no contract" in result.remarks
    assert fyers.calls == []


def test_missing_option_details_is_rejected(repo: ExecutionRepository) -> None:
    fyers = FakeFyers(response=_ok("1"))
    result = _executor(repo, fyers).execute(_order(underlying=None))

    assert result.status is ExecutionStatus.REJECTED
    assert "option details" in result.remarks
    assert fyers.calls == []


def test_instrument_fetch_failure_is_a_failure(repo: ExecutionRepository) -> None:
    resolver = FakeResolver(error=RuntimeError("master download failed"))
    result = _executor(repo, FakeFyers(response=_ok("1")), resolver).execute(_order())
    assert result.status is ExecutionStatus.FAILED


def test_limit_order_includes_price(repo: ExecutionRepository) -> None:
    fyers = FakeFyers(response=_ok("1"))
    _executor(repo, fyers).execute(_order(order_type=OrderType.LIMIT, entry_price=150.0))

    (params,) = fyers.calls
    assert params["type"] == 1  # LIMIT
    assert params["limitPrice"] == 150.0


def test_product_codes_map_to_fyers(repo: ExecutionRepository) -> None:
    fyers = FakeFyers(response=_ok("1"))
    ex = _executor(repo, fyers)
    ex.execute(_order(product=ProductType.MARGIN))
    ex.execute(_order(product=ProductType.DELIVERY))
    assert fyers.calls[0]["productType"] == "MARGIN"
    assert fyers.calls[1]["productType"] == "CNC"


# --- Invalid order (shared validation gate, before any broker call) -----------


def test_invalid_order_is_rejected_without_calling_broker(repo: ExecutionRepository) -> None:
    fyers = FakeFyers(response=_ok("1"))
    result = _executor(repo, fyers).execute(_order(quantity=0))

    assert result.status is ExecutionStatus.REJECTED
    assert "quantity must be positive" in result.remarks
    assert fyers.calls == []


# --- Rejected / error responses (FYERS reports these in the dict) -------------


def test_rejected_order_is_rejected(repo: ExecutionRepository) -> None:
    fyers = FakeFyers(response=_err("Order rejected by RMS"))
    result = _executor(repo, fyers).execute(_order())

    assert result.status is ExecutionStatus.REJECTED
    assert "Order rejected by FYERS" in result.remarks
    assert result.broker_order_id is None


def test_insufficient_margin_is_rejected(repo: ExecutionRepository) -> None:
    fyers = FakeFyers(response=_err("Insufficient funds for this order"))
    result = _executor(repo, fyers).execute(_order())

    assert result.status is ExecutionStatus.REJECTED
    assert "Insufficient margin" in result.remarks


def test_market_closed_is_rejected(repo: ExecutionRepository) -> None:
    fyers = FakeFyers(response=_err("Market is closed"))
    result = _executor(repo, fyers).execute(_order())

    assert result.status is ExecutionStatus.REJECTED
    assert "Market closed" in result.remarks


def test_success_response_without_id_is_rejected(repo: ExecutionRepository) -> None:
    fyers = FakeFyers(response={"s": "ok", "message": "queued"})  # no id
    result = _executor(repo, fyers).execute(_order())
    assert result.status is ExecutionStatus.REJECTED
    assert "no order id" in result.remarks


# --- Raised transport failures ------------------------------------------------


def test_network_timeout_fails(repo: ExecutionRepository) -> None:
    fyers = FakeFyers(error=TimeoutError("read timed out"))
    result = _executor(repo, fyers).execute(_order())

    assert result.status is ExecutionStatus.FAILED
    assert "network" in result.remarks.lower()


def test_rate_limit_response_fails(repo: ExecutionRepository) -> None:
    fyers = FakeFyers(response=_err("Too many requests, rate limit exceeded"))
    result = _executor(repo, fyers).execute(_order())

    assert result.status is ExecutionStatus.FAILED
    assert "rate limit" in result.remarks.lower()


def test_unexpected_exception_fails_gracefully(repo: ExecutionRepository) -> None:
    fyers = FakeFyers(error=RuntimeError("boom"))
    result = _executor(repo, fyers).execute(_order())

    assert result.status is ExecutionStatus.FAILED
    assert "Unexpected error" in result.remarks


# --- Persistence & logging ----------------------------------------------------


def test_every_attempt_is_persisted(repo: ExecutionRepository) -> None:
    _executor(repo, FakeFyers(response=_ok("ID1"))).execute(_order())
    _executor(repo, FakeFyers(response=_err("rejected"))).execute(_order())

    stored = repo.list_all()
    assert len(stored) == 2
    assert stored[0].status is ExecutionStatus.SUCCESS
    assert stored[0].broker_order_id == "ID1"
    assert stored[1].status is ExecutionStatus.REJECTED
    assert stored[1].broker_order_id is None


def test_attempt_is_logged_with_duration(
    repo: ExecutionRepository, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="teletrader.execution.fyers"):
        _executor(repo, FakeFyers(response=_ok("ID9"))).execute(_order())
    log = caplog.text
    assert "[FYERS]" in log
    assert "BUY NIFTY 23900 PE" in log
    assert "symbol=NSE:NIFTY2570323900PE" in log
    assert "status=SUCCESS" in log
    assert "duration=" in log
    assert "broker_order_id=ID9" in log


def test_secrets_are_never_logged(
    repo: ExecutionRepository, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        _executor(repo, FakeFyers(response=_ok("ID9"))).execute(_order())
    assert "access_token" not in caplog.text.lower()


# --- Missing credentials (live build path) ------------------------------------


def test_missing_credentials_raises_authentication_error(repo: ExecutionRepository) -> None:
    from teletrader.execution.exceptions import AuthenticationError

    with pytest.raises(AuthenticationError):
        FyersExecutor(repo, app_id=None, access_token=None)


# --- Error translation (unit) -------------------------------------------------


def test_translate_exception_covers_categories() -> None:
    from teletrader.execution.exceptions import (
        AuthenticationError,
        BrokerCommunicationError,
        RateLimitError,
    )

    assert isinstance(translate_fyers_exception(TimeoutError("t")), BrokerCommunicationError)
    assert isinstance(
        translate_fyers_exception(RuntimeError("too many requests")), RateLimitError
    )
    assert isinstance(
        translate_fyers_exception(RuntimeError("invalid token")), AuthenticationError
    )
    assert isinstance(translate_fyers_exception(RuntimeError("?")), BrokerCommunicationError)


def test_error_from_response_covers_categories() -> None:
    from teletrader.execution.exceptions import (
        AuthenticationError,
        InsufficientMarginError,
        OrderRejectedError,
        RateLimitError,
    )

    assert _error_from_response({"s": "ok", "id": "1"}) is None
    assert isinstance(_error_from_response(_err("low margin funds")), InsufficientMarginError)
    assert isinstance(_error_from_response(_err("Market not open")), OrderRejectedError)
    assert isinstance(_error_from_response(_err("invalid token")), AuthenticationError)
    assert isinstance(_error_from_response(_err("too many requests")), RateLimitError)
    assert isinstance(_error_from_response(_err("plain reject")), OrderRejectedError)


# --- The Trade Engine must never know about FYERS -----------------------------


def test_trade_engine_has_no_fyers_dependency() -> None:
    import teletrader.trade_engine as engine_module

    # The engine may name brokers in prose, but must not import or expose any
    # FYERS type — the decision layer stays broker-agnostic.
    source = inspect.getsource(engine_module)
    import_lines = [
        line for line in source.splitlines()
        if line.strip().startswith(("import ", "from "))
    ]
    assert not any("fyers" in line.lower() for line in import_lines)
    assert not hasattr(engine_module, "FyersExecutor")


# --- Protective SL-M order translation ----------------------------------------


def test_sl_m_order_includes_stop_price(repo: ExecutionRepository) -> None:
    fyers = FakeFyers(response=_ok("SL1"))
    sl_order = _order(
        transaction_type=TransactionType.SELL,
        order_type=OrderType.SL_M,
        entry_price=None,
        stop_loss=None,
        target=None,
        trigger_price=150.0,
    )
    result = _executor(repo, fyers).execute(sl_order)

    assert result.status is ExecutionStatus.SUCCESS
    (params,) = fyers.calls
    assert params["type"] == 3  # SL-M
    assert params["stopPrice"] == 150.0
    assert params["side"] == -1  # SELL
    assert params["limitPrice"] == 0


# --- Order state (fill detection + OCO) ---------------------------------------


def test_get_order_state_maps_filled_with_fill(repo: ExecutionRepository) -> None:
    fyers = FakeFyers(book=[{"id": "OID", "status": 2, "tradedPrice": 165.5, "filledQty": 75}])
    state = _executor(repo, fyers).get_order_state("OID")
    assert state.status is OrderStatus.COMPLETE
    assert state.is_filled
    assert state.average_price == 165.5
    assert state.filled_quantity == 75


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (6, OrderStatus.PENDING),
        (4, OrderStatus.PENDING),
        (5, OrderStatus.REJECTED),
        (1, OrderStatus.CANCELLED),
        (99, OrderStatus.PENDING),  # unknown code -> safe PENDING
    ],
)
def test_get_order_state_maps_status(repo: ExecutionRepository, raw: int, expected) -> None:
    fyers = FakeFyers(book=[{"id": "OID", "status": raw}])
    assert _executor(repo, fyers).get_order_state("OID").status is expected


def test_get_order_state_unknown_for_missing_or_empty(repo: ExecutionRepository) -> None:
    ex = _executor(repo, FakeFyers(book=[]))
    assert ex.get_order_state("OID").status is OrderStatus.UNKNOWN
    assert ex.get_order_state(None).status is OrderStatus.UNKNOWN


def test_get_order_state_never_raises(repo: ExecutionRepository) -> None:
    class Boom(FakeFyers):
        def orderbook(self, data: dict[str, object] | None = None) -> dict[str, object]:
            raise ConnectionError("down")

    assert _executor(repo, Boom()).get_order_state("OID").status is OrderStatus.UNKNOWN


# --- Cancel / modify (management ops) -----------------------------------------


def test_cancel_order_calls_fyers(repo: ExecutionRepository) -> None:
    fyers = FakeFyers()
    result = _executor(repo, fyers).cancel_order("OID", symbol="NIFTY 23900 PE")
    assert result.action is ManagementAction.CANCEL
    assert result.succeeded
    assert fyers.cancelled == [{"id": "OID"}]


def test_cancel_order_without_id_fails(repo: ExecutionRepository) -> None:
    result = _executor(repo, FakeFyers()).cancel_order(None)
    assert not result.succeeded
    assert "No broker order id" in result.remarks


def test_cancel_order_translates_error_response(repo: ExecutionRepository) -> None:
    fyers = FakeFyers(cancel_response=_err("order is not open"))
    result = _executor(repo, fyers).cancel_order("OID")
    assert not result.succeeded


def test_modify_stop_loss_calls_fyers(repo: ExecutionRepository) -> None:
    fyers = FakeFyers()
    result = _executor(repo, fyers).modify_stop_loss("SL1", 165.0)
    assert result.action is ManagementAction.MODIFY_STOP_LOSS
    assert result.succeeded
    assert fyers.modified == [{"id": "SL1", "type": 3, "stopPrice": 165.0}]


def test_modify_target_calls_fyers(repo: ExecutionRepository) -> None:
    fyers = FakeFyers()
    result = _executor(repo, fyers).modify_target("TG1", 250.0)
    assert result.action is ManagementAction.MODIFY_TARGET
    assert result.succeeded
    assert fyers.modified == [{"id": "TG1", "type": 1, "limitPrice": 250.0}]


def test_importing_decision_layer_does_not_load_fyers_sdk() -> None:
    # Building the decision + execution layer must not drag in the heavy FYERS SDK;
    # it is loaded lazily, only when a real FyersExecutor client is constructed.
    import subprocess

    code = (
        "import sys, teletrader.pipeline, teletrader.trade_engine, teletrader.execution;"
        "assert 'fyers_apiv3' not in sys.modules, sorted(m for m in sys.modules if 'fyers' in m)"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
