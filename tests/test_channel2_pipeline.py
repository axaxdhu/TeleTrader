"""Unit tests for the channel-2 parse-only pipeline and source-scoped storage.

Channel 2 parses + stores but does not trade, and its signals are isolated from
channel 1's via the repository's ``source`` scoping (so, e.g., the trade engine's
counts never see channel-2 rows).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from teletrader.database import connect, initialize
from teletrader.pipeline import Channel2Pipeline, PipelineStatus
from teletrader.repository import SignalRepository

WHEN = datetime(2026, 7, 3, 4, 30, tzinfo=timezone.utc)
C2_SIGNAL = "Nifty 23900 pe above 128\n\nLot 65\n\nTarget 140, 155, 175++\n\nSl 115\n\nCmp 124"


@pytest.fixture
def connection() -> sqlite3.Connection:
    conn = connect(":memory:")
    initialize(conn)
    yield conn
    conn.close()


def _pipeline(connection: sqlite3.Connection) -> Channel2Pipeline:
    return Channel2Pipeline(
        SignalRepository(connection, tz=timezone.utc, source="channel2")
    )


def test_noise_is_ignored(connection: sqlite3.Connection) -> None:
    result = _pipeline(connection).process("sl hit on this", when=WHEN)
    assert result.status is PipelineStatus.IGNORED


def test_valid_signal_is_stored(connection: sqlite3.Connection) -> None:
    result = _pipeline(connection).process(C2_SIGNAL, when=WHEN)
    assert result.status is PipelineStatus.STORED
    assert result.stored_id is not None
    assert result.signal is not None
    assert result.signal.target == 140.0  # first target, raw

    stored = SignalRepository(connection, source="channel2").list_all()
    assert len(stored) == 1
    assert stored[0].source == "channel2"


def test_same_day_repeat_is_duplicate(connection: sqlite3.Connection) -> None:
    pipe = _pipeline(connection)
    assert pipe.process(C2_SIGNAL, when=WHEN).status is PipelineStatus.STORED
    assert pipe.process(C2_SIGNAL, when=WHEN).status is PipelineStatus.DUPLICATE


def test_sources_are_isolated(connection: sqlite3.Connection) -> None:
    """Channel-2 rows must not appear in a channel-1-scoped repository, and
    each channel dedupes within itself."""
    ch2 = SignalRepository(connection, tz=timezone.utc, source="channel2")
    ch1 = SignalRepository(connection, tz=timezone.utc, source="channel1")

    Channel2Pipeline(ch2).process(C2_SIGNAL, when=WHEN)

    assert ch2.count() == 1
    assert ch1.count() == 0  # channel 1 sees none of channel 2's storage
    # And channel-1 counting (used by the engine's daily limit) excludes ch2.
    assert ch1.count_since(WHEN.replace(hour=0, minute=0)) == 0


# --- Shadow mode: channel 2 on its way to live trading ------------------------
#
# Channel 2 now runs the same stages as channel 1 (engine included) but with a
# shadow executor, so these tests pin the behaviour the ramp-up depends on: the
# engine's gates really apply, the status distinguishes a withheld order from a
# placed one, and a message that looks like a signal but does not parse is
# surfaced instead of dropped.

from teletrader.config import Config  # noqa: E402
from teletrader.execution import (  # noqa: E402
    ExecutionResult,
    ExecutionStatus,
    OrderRequest,
    ShadowReport,
)
from teletrader.trade_engine import TradeEngine  # noqa: E402
from datetime import date, time  # noqa: E402


class RecordingExecutor:
    """An executor that records orders and reports them as shadowed or placed."""

    def __init__(self, *, shadow: bool = True) -> None:
        self._shadow = shadow
        self.orders: list[OrderRequest] = []

    @property
    def mode(self) -> str:
        return "fyers_shadow" if self._shadow else "fyers"

    def execute(self, order: OrderRequest) -> ExecutionResult:
        self.orders.append(order)
        report = (
            ShadowReport(
                tradingsymbol="NSE:NIFTY26O0323900PE",
                exchange="NSE",
                expiry=date(2026, 10, 3),
                lot_size=65,
                lots=1,
                quantity=order.quantity,
                payload={"symbol": "NSE:NIFTY26O0323900PE", "qty": order.quantity},
                funds_required=8_320.0,
                funds_available=50_000.0,
                funds_ok=True,
                funds_note="Sufficient funds.",
            )
            if self._shadow
            else None
        )
        return ExecutionResult(
            status=ExecutionStatus.SUCCESS,
            order=order,
            remarks="ok",
            timestamp=WHEN,
            shadow=report,
        )


def _config(**overrides: object) -> Config:
    params: dict[str, object] = {
        "api_id": 1,
        "api_hash": "hash",
        "phone": "+10000000000",
        "session_name": "test",
        "channel": "me",
        "database_path": ":memory:",
        "log_level": "INFO",
        "allow_duplicates": False,
        "max_trades_per_day": 100,
        "trade_lots": 1,
        "execution_mode": "fyers_shadow",
        "kite_api_key": None,
        "kite_api_secret": None,
        "kite_access_token": None,
        "auto_trading": True,
        "lot_sizes": {"NIFTY": 65},
        # A window wide enough that the market-hours rule never decides these
        # tests; the gating itself is exercised explicitly below.
        "market_open": time(0, 0),
        "market_close": time(23, 59),
        "market_timezone": "UTC",
    }
    params.update(overrides)
    return Config(**params)  # type: ignore[arg-type]


def _trading_pipeline(
    connection: sqlite3.Connection,
    *,
    executor: RecordingExecutor | None = None,
    config: Config | None = None,
) -> tuple[Channel2Pipeline, RecordingExecutor]:
    repository = SignalRepository(connection, tz=timezone.utc, source="channel2")
    conf = config or _config()
    executor = executor or RecordingExecutor()
    engine = TradeEngine(conf, repository, clock=lambda: WHEN)
    return Channel2Pipeline(repository, engine, executor), executor


def test_accepted_signal_is_shadowed_not_executed(
    connection: sqlite3.Connection,
) -> None:
    pipeline, executor = _trading_pipeline(connection)

    result = pipeline.process(C2_SIGNAL, when=WHEN)

    # SHADOWED, never EXECUTED: the status must not imply money moved.
    assert result.status is PipelineStatus.SHADOWED
    assert result.execution is not None
    assert result.execution.shadow is not None
    assert len(executor.orders) == 1
    assert executor.orders[0].quantity == 65  # 1 lot x NIFTY lot size


def test_order_carries_the_channel2_signal_prices(
    connection: sqlite3.Connection,
) -> None:
    pipeline, executor = _trading_pipeline(connection)

    pipeline.process(C2_SIGNAL, when=WHEN)

    order = executor.orders[0]
    assert order.entry_price == 128.0
    assert order.stop_loss == 115.0
    assert order.target == 140.0  # first target, taken raw (no channel-1 offset)


def test_engine_gates_apply_to_channel_2(connection: sqlite3.Connection) -> None:
    # Auto-trading off must stop a channel-2 signal exactly as it stops
    # channel 1's — the shadow run should reflect the real gating.
    pipeline, executor = _trading_pipeline(
        connection, config=_config(auto_trading=False)
    )

    result = pipeline.process(C2_SIGNAL, when=WHEN)

    assert result.status is PipelineStatus.NOT_TRADED
    assert executor.orders == []


def test_a_live_executor_reports_executed_not_shadowed(
    connection: sqlite3.Connection,
) -> None:
    # The status follows what the executor actually did, so switching the channel
    # to a live broker cannot keep reporting reassuring "shadow" results.
    pipeline, _ = _trading_pipeline(connection, executor=RecordingExecutor(shadow=False))

    result = pipeline.process(C2_SIGNAL, when=WHEN)

    assert result.status is PipelineStatus.EXECUTED


def test_signal_like_message_that_fails_to_parse_is_reported(
    connection: sqlite3.Connection,
) -> None:
    pipeline, executor = _trading_pipeline(connection)

    # A real entry shape with the stop written in a way the parser cannot read.
    result = pipeline.process("Coforge 1500 ce above 70\nsl-- sixty six", when=WHEN)

    assert result.status is PipelineStatus.MISSED
    assert result.raw_text is not None and "Coforge" in result.raw_text
    assert executor.orders == []  # a miss never trades


def test_ordinary_chatter_is_still_ignored(connection: sqlite3.Connection) -> None:
    pipeline, _ = _trading_pipeline(connection)

    for noise in ("sl hit on this", "🔥 Coforge 1500 ce Safe target Done ✔️", "One more"):
        assert pipeline.process(noise, when=WHEN).status is PipelineStatus.IGNORED


def test_parse_only_mode_still_works_without_an_engine(
    connection: sqlite3.Connection,
) -> None:
    # The original parse-only shape is preserved for a channel that should not
    # reach an executor at all.
    result = _pipeline(connection).process(C2_SIGNAL, when=WHEN)

    assert result.status is PipelineStatus.STORED
