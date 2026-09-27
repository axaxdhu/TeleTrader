"""Unit tests for :class:`FyersShadowExecutor` — the live path, minus the submit.

The executor's whole value is a promise: it walks the real FYERS path (same
validation, same symbol resolution, same payload) and never places an order. Two
things therefore matter most here and are asserted hardest — that
``place_order`` is *never* called on any path, and that what it reports back is
the real payload and the real contract rather than a reconstruction.

No network call is ever made: a fake FYERS client and a fake resolver are
injected, exactly as in the live executor's tests.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, timezone

import pytest

from teletrader.database import connect, initialize
from teletrader.execution import (
    ExecutionRepository,
    ExecutionStatus,
    Executor,
    FyersShadowExecutor,
    InstrumentNotFoundError,
    OrderRequest,
    OrderStatus,
    OrderType,
    ProductType,
    ResolvedInstrument,
    TransactionType,
)
from teletrader.execution.shadow import MODE, _available_from_funds

NOW = datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)

RESOLVED = ResolvedInstrument(
    tradingsymbol="NSE:COFORGE26OCT1500CE",
    exchange="NSE",
    expiry=date(2026, 10, 29),
    lot_size=150,
)


# --- Fakes / builders ---------------------------------------------------------


class ExplodingFyers:
    """A client that fails the test if the executor ever tries to trade.

    Shadow mode's guarantee is "no order reaches the broker"; making the attempt
    itself an error means a regression cannot pass quietly.
    """

    def __init__(self, funds: object | Exception | None = None) -> None:
        self._funds = funds
        self.funds_calls = 0

    def place_order(self, data: dict[str, object]) -> dict[str, object]:
        raise AssertionError("shadow mode must never call place_order")

    def modify_order(self, data: dict[str, object]) -> dict[str, object]:
        raise AssertionError("shadow mode must never call modify_order")

    def cancel_order(self, data: dict[str, object]) -> dict[str, object]:
        raise AssertionError("shadow mode must never call cancel_order")

    def funds(self) -> object:
        self.funds_calls += 1
        if isinstance(self._funds, Exception):
            raise self._funds
        return self._funds


class FundlessFyers(ExplodingFyers):
    """A client with no ``funds`` endpoint at all (older/limited SDK surface)."""

    funds = None  # type: ignore[assignment]


class FakeResolver:
    """Returns a fixed contract (or raises), recording the resolve arguments."""

    def __init__(
        self,
        *,
        instrument: ResolvedInstrument | None = RESOLVED,
        error: Exception | None = None,
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


def _funds(available: float) -> dict[str, object]:
    """A realistic FYERS ``funds()`` response with the labelled balance bucket."""
    return {
        "s": "ok",
        "fund_limit": [
            {"id": 1, "title": "Total Balance", "equityAmount": available * 2},
            {"id": 10, "title": "Available Balance", "equityAmount": available},
        ],
    }


def _order(**overrides: object) -> OrderRequest:
    params: dict[str, object] = {
        "symbol": "COFORGE 1500 CE",
        "transaction_type": TransactionType.BUY,
        "quantity": 150,
        "order_type": OrderType.MARKET,
        "product": ProductType.INTRADAY,
        "entry_price": 70.0,
        "stop_loss": 66.0,
        "target": 73.0,
        "signal_id": None,  # no stored signal in these unit tests (FK)
        "underlying": "COFORGE",
        "strike": 1500,
        "option_type": "CE",
    }
    params.update(overrides)
    return OrderRequest(**params)  # type: ignore[arg-type]


@pytest.fixture()
def connection() -> sqlite3.Connection:
    conn = connect(":memory:")
    initialize(conn)
    yield conn
    conn.close()


@pytest.fixture()
def repository(connection: sqlite3.Connection) -> ExecutionRepository:
    return ExecutionRepository(connection)


def _executor(
    repository: ExecutionRepository,
    *,
    client: object | None = None,
    resolver: FakeResolver | None = None,
) -> FyersShadowExecutor:
    return FyersShadowExecutor(
        repository,
        client=client or ExplodingFyers(funds=_funds(50_000.0)),
        resolver=resolver or FakeResolver(),
        tz=timezone.utc,
        clock=lambda: NOW,
    )


# --- The core promise: nothing is ever sent -----------------------------------


def test_successful_shadow_run_never_places_an_order(
    repository: ExecutionRepository,
) -> None:
    client = ExplodingFyers(funds=_funds(50_000.0))
    # ExplodingFyers raises on place_order, so completing at all proves the point.
    result = _executor(repository, client=client).execute(_order())

    assert result.status is ExecutionStatus.SUCCESS
    assert result.broker_order_id is None
    assert "Nothing was sent" in result.remarks


def test_mode_is_distinct_from_the_live_broker(
    repository: ExecutionRepository,
) -> None:
    # The mode string is what the pipeline reports and what config selects; it
    # must never read as the live broker.
    assert _executor(repository).mode == MODE == "fyers_shadow"


def test_satisfies_the_executor_interface(repository: ExecutionRepository) -> None:
    assert isinstance(_executor(repository), Executor)


# --- What it reports ----------------------------------------------------------


def test_reports_the_resolved_contract_and_quantity(
    repository: ExecutionRepository,
) -> None:
    result = _executor(repository).execute(_order())

    report = result.shadow
    assert report is not None
    assert report.tradingsymbol == "NSE:COFORGE26OCT1500CE"
    assert report.exchange == "NSE"
    assert report.expiry == date(2026, 10, 29)
    assert report.lot_size == 150
    assert report.quantity == 150
    assert report.lots == 1


def test_reports_the_real_payload_that_would_have_been_sent(
    repository: ExecutionRepository,
) -> None:
    result = _executor(repository).execute(_order())

    assert result.shadow is not None
    payload = result.shadow.payload
    # The payload is the live executor's own translation: FYERS numeric codes,
    # the resolved symbol, and the quantity — not a human-readable summary.
    assert payload["symbol"] == "NSE:COFORGE26OCT1500CE"
    assert payload["qty"] == 150
    assert payload["side"] == 1  # buy
    assert payload["type"] == 2  # market


def test_records_the_attempt_with_the_resolved_contract(
    repository: ExecutionRepository,
) -> None:
    _executor(repository).execute(_order())

    stored = repository.list_all()
    assert len(stored) == 1
    # The executions table stores the verdict rather than the raw request, so the
    # contract has to survive in the remarks — otherwise weeks of shadow history
    # would not say what would have been bought.
    assert "NSE:COFORGE26OCT1500CE" in (stored[0].remarks or "")
    assert stored[0].status is ExecutionStatus.SUCCESS


def test_logs_the_shadow_block(
    repository: ExecutionRepository, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        _executor(repository).execute(_order())

    assert "[SHADOW] NOT SENT" in caplog.text
    assert "NSE:COFORGE26OCT1500CE" in caplog.text


# --- Funds check --------------------------------------------------------------


def test_sufficient_funds_pass(repository: ExecutionRepository) -> None:
    result = _executor(
        repository, client=ExplodingFyers(funds=_funds(50_000.0))
    ).execute(_order())

    report = result.shadow
    assert report is not None
    assert report.funds_required == pytest.approx(10_500.0)  # 70 x 150
    assert report.funds_available == pytest.approx(50_000.0)
    assert report.funds_ok is True
    assert result.status is ExecutionStatus.SUCCESS


def test_insufficient_funds_is_reported_as_would_not_go_through(
    repository: ExecutionRepository,
) -> None:
    result = _executor(
        repository, client=ExplodingFyers(funds=_funds(1_000.0))
    ).execute(_order())

    assert result.status is ExecutionStatus.REJECTED
    assert result.shadow is not None
    assert result.shadow.funds_ok is False
    assert "Would NOT go through" in result.remarks
    assert "insufficient funds" in result.remarks


def test_unreadable_balance_is_unknown_not_a_pass(
    repository: ExecutionRepository,
) -> None:
    result = _executor(
        repository, client=ExplodingFyers(funds=RuntimeError("token expired"))
    ).execute(_order())

    report = result.shadow
    assert report is not None
    # Unknown must never masquerade as verified — but it also must not block the
    # report, whose job is to show the payload.
    assert report.funds_ok is None
    assert report.funds_available is None
    assert "unavailable" in report.funds_note
    assert result.status is ExecutionStatus.SUCCESS


def test_client_without_a_funds_endpoint_degrades(
    repository: ExecutionRepository,
) -> None:
    result = _executor(repository, client=FundlessFyers()).execute(_order())

    assert result.shadow is not None
    assert result.shadow.funds_ok is None


def test_sell_skips_the_funds_check(repository: ExecutionRepository) -> None:
    # A short option is margined by span/exposure rules this app does not model,
    # so the check is skipped rather than guessed.
    result = _executor(repository).execute(
        _order(transaction_type=TransactionType.SELL)
    )

    assert result.shadow is not None
    assert result.shadow.funds_ok is None
    assert "not a buy" in result.shadow.funds_note


@pytest.mark.parametrize(
    "response",
    [
        {"s": "error", "message": "invalid token"},
        {"s": "ok", "fund_limit": "nonsense"},
        {"s": "ok", "fund_limit": [{"title": "Total Balance", "equityAmount": 5}]},
        {"s": "ok", "fund_limit": [{"title": "Available Balance", "equityAmount": "x"}]},
        "not a dict",
        None,
    ],
)
def test_malformed_funds_responses_read_as_unknown(response: object) -> None:
    assert _available_from_funds(response) is None


def test_available_balance_is_read_from_the_labelled_bucket() -> None:
    assert _available_from_funds(_funds(1_234.5)) == pytest.approx(1_234.5)


# --- Failures on the way to the door ------------------------------------------


def test_unresolvable_symbol_is_rejected_without_a_report(
    repository: ExecutionRepository,
) -> None:
    resolver = FakeResolver(error=InstrumentNotFoundError("no such contract"))
    result = _executor(repository, resolver=resolver).execute(_order())

    # It would not have reached FYERS at all — that *is* the finding, and there
    # is no payload to show.
    assert result.status is ExecutionStatus.REJECTED
    assert result.shadow is None
    assert "no such contract" in result.remarks


def test_invalid_order_is_rejected_before_resolution(
    repository: ExecutionRepository,
) -> None:
    resolver = FakeResolver()
    result = _executor(repository, resolver=resolver).execute(_order(quantity=0))

    assert result.status is ExecutionStatus.REJECTED
    assert resolver.calls == []  # never even looked the contract up


def test_order_state_is_unknown_because_nothing_was_placed(
    repository: ExecutionRepository,
) -> None:
    state = _executor(repository).get_order_state("whatever")

    assert state.status is OrderStatus.UNKNOWN
