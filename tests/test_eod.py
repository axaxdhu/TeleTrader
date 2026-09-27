"""Unit tests for end-of-day scoring — what the day's shadow trades were worth.

This is the module whose output the user will act on ("is this channel worth
trading?"), so the tests lean hardest on the ways a P&L can flatter itself: a
bar that spans both the stop and the target, a trade that never resolves, a
contract whose data cannot be fetched, and candles from before the signal
arrived. Each must come out pessimistic or unscored — never optimistic.

No network: candles are supplied by a fake source.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from teletrader.database import connect, initialize
from teletrader.eod import (
    Candle,
    FyersCandleSource,
    ScoredTrade,
    build_report,
    format_report,
    score_day,
    score_run,
)
from teletrader.shadow_repository import (
    Outcome,
    ShadowRepository,
    ShadowRun,
    StoredShadowRun,
)

DAY = date(2026, 9, 28)
OPEN_TIME = datetime(2026, 9, 28, 9, 15, tzinfo=timezone.utc)
SIGNAL_TIME = datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)


def _run(**overrides: object) -> StoredShadowRun:
    detail: dict[str, object] = {
        "source": "channel2",
        "tradingsymbol": "NSE:COFORGE26OCT1500CE",
        "exchange": "NSE",
        "expiry": date(2026, 10, 29),
        "quantity": 100,
        "lot_size": 100,
        "accepted": True,
        "protected": True,
        "underlying": "COFORGE",
        "entry_price": 70.0,
        "stop_loss": 66.0,
        "target": 73.0,
    }
    detail.update(overrides)
    return StoredShadowRun(
        id=1,
        trade_date=DAY.isoformat(),
        created_at=SIGNAL_TIME,
        run=ShadowRun(**detail),  # type: ignore[arg-type]
    )


def _candle(minute: int, low: float, high: float, close: float | None = None) -> Candle:
    return Candle(
        start=SIGNAL_TIME + timedelta(minutes=minute),
        open=(low + high) / 2,
        high=high,
        low=low,
        close=close if close is not None else (low + high) / 2,
    )


# --- The three outcomes -------------------------------------------------------


def test_target_reached_is_priced_at_the_target() -> None:
    scored = score_run(_run(), [_candle(1, 69, 71), _candle(2, 70, 74)])

    assert scored.outcome is Outcome.TARGET
    assert scored.exit_price == 73.0
    assert scored.pnl == pytest.approx((73.0 - 70.0) * 100)


def test_stop_hit_is_priced_at_the_stop() -> None:
    scored = score_run(_run(), [_candle(1, 69, 71), _candle(2, 65, 69)])

    assert scored.outcome is Outcome.STOPPED
    assert scored.pnl == pytest.approx((66.0 - 70.0) * 100)
    assert scored.pnl < 0


def test_neither_hit_is_marked_to_the_close() -> None:
    scored = score_run(_run(), [_candle(1, 69, 71), _candle(2, 69, 72, close=71.5)])

    assert scored.outcome is Outcome.OPEN
    assert scored.exit_price == 71.5
    assert scored.pnl == pytest.approx((71.5 - 70.0) * 100)


def test_whichever_comes_first_wins() -> None:
    # The stop is hit in bar 1; a later bar reaching the target must not
    # retroactively rescue a trade that was already closed out.
    scored = score_run(_run(), [_candle(1, 65, 69), _candle(2, 70, 80)])

    assert scored.outcome is Outcome.STOPPED


# --- The pessimism rules ------------------------------------------------------


def test_a_bar_spanning_both_counts_as_stopped() -> None:
    # A minute candle records a range, not the order its extremes occurred in.
    # Reading it the flattering way is how a backtest lies.
    scored = score_run(_run(), [_candle(1, 65, 80)])

    assert scored.outcome is Outcome.STOPPED
    assert scored.pnl is not None and scored.pnl < 0


def test_candles_before_the_signal_are_ignored() -> None:
    # The signal arrived at 10:00; a stop touched at 09:30 is not this trade's.
    early = Candle(start=OPEN_TIME, open=70, high=80, low=60, close=70)
    scored = score_run(_run(), [early, _candle(1, 69, 71, close=70.5)])

    assert scored.outcome is Outcome.OPEN
    assert scored.exit_price == 70.5


def test_no_market_data_is_unscored_not_flat() -> None:
    scored = score_run(_run(), [])

    # Unknown must never be reported as a break-even trade.
    assert scored.outcome is Outcome.UNKNOWN
    assert scored.pnl is None


def test_a_rejected_order_is_not_priced() -> None:
    scored = score_run(_run(accepted=False), [_candle(1, 70, 80)])

    assert scored.outcome is Outcome.UNKNOWN
    assert scored.pnl is None
    assert "not have been accepted" in scored.note


def test_a_trade_with_no_stop_can_still_reach_its_target() -> None:
    scored = score_run(_run(stop_loss=None), [_candle(1, 10, 74)])

    assert scored.outcome is Outcome.TARGET


# --- Scoring a whole day ------------------------------------------------------


class FakeCandles:
    """Serves canned candles, or raises for a chosen symbol."""

    def __init__(
        self, data: dict[str, list[Candle]], *, failing: str | None = None
    ) -> None:
        self._data = data
        self._failing = failing
        self.calls: list[str] = []

    def candles(self, tradingsymbol: str, day: date) -> list[Candle]:
        self.calls.append(tradingsymbol)
        if tradingsymbol == self._failing:
            raise RuntimeError("history unavailable")
        return self._data.get(tradingsymbol, [])


@pytest.fixture()
def connection() -> sqlite3.Connection:
    conn = connect(":memory:")
    initialize(conn)
    yield conn
    conn.close()


@pytest.fixture()
def repository(connection: sqlite3.Connection) -> ShadowRepository:
    return ShadowRepository(connection, tz=timezone.utc)


def _store(repository: ShadowRepository, **overrides: object) -> int:
    detail: dict[str, object] = {
        "source": "channel2",
        "tradingsymbol": "NSE:COFORGE26OCT1500CE",
        "exchange": "NSE",
        "expiry": date(2026, 10, 29),
        "quantity": 100,
        "lot_size": 100,
        "accepted": True,
        "protected": True,
        "underlying": "COFORGE",
        "entry_price": 70.0,
        "stop_loss": 66.0,
        "target": 73.0,
    }
    detail.update(overrides)
    return repository.add(ShadowRun(**detail), created_at=SIGNAL_TIME)  # type: ignore[arg-type]


def test_day_is_scored_and_totalled(repository: ShadowRepository) -> None:
    _store(repository)
    _store(repository, tradingsymbol="NSE:NIFTY26OCT23300PE", underlying="NIFTY",
           quantity=65, lot_size=65, entry_price=131.0, stop_loss=120.0, target=142.0)
    candles = FakeCandles(
        {
            "NSE:COFORGE26OCT1500CE": [_candle(1, 70, 74)],           # target
            "NSE:NIFTY26OCT23300PE": [_candle(1, 119, 132)],          # stopped
        }
    )

    report = score_day(repository, candles, day=DAY)

    assert report.count(Outcome.TARGET) == 1
    assert report.count(Outcome.STOPPED) == 1
    assert report.total_pnl == pytest.approx((73 - 70) * 100 + (120 - 131) * 65)


def test_outcomes_are_persisted(repository: ShadowRepository) -> None:
    run_id = _store(repository)
    score_day(repository, FakeCandles({"NSE:COFORGE26OCT1500CE": [_candle(1, 70, 74)]}), day=DAY)

    stored = repository.for_day(DAY)[0]
    assert stored.id == run_id
    assert stored.scored is True
    assert stored.outcome is Outcome.TARGET
    assert stored.pnl == pytest.approx(300.0)


def test_a_failed_fetch_leaves_the_trade_unscored_not_missing(
    repository: ShadowRepository,
) -> None:
    _store(repository)
    report = score_day(
        repository, FakeCandles({}, failing="NSE:COFORGE26OCT1500CE"), day=DAY
    )

    # The trade count must still match reality even when data is missing.
    assert len(report.trades) == 1
    assert report.trades[0].pnl is None
    assert report.total_pnl == 0.0


def test_candles_are_fetched_once_per_symbol(repository: ShadowRepository) -> None:
    _store(repository)
    _store(repository, entry_price=71.0)  # same contract, second signal
    candles = FakeCandles({"NSE:COFORGE26OCT1500CE": [_candle(1, 70, 74)]})

    score_day(repository, candles, day=DAY)

    assert candles.calls == ["NSE:COFORGE26OCT1500CE"]


def test_other_channels_are_not_included(repository: ShadowRepository) -> None:
    _store(repository, source="channel1")
    report = score_day(repository, FakeCandles({}), day=DAY, source="channel2")

    assert report.trades == ()


# --- The summary --------------------------------------------------------------


def test_summary_shows_each_trade_and_the_total(repository: ShadowRepository) -> None:
    _store(repository)
    text = build_report(
        repository, FakeCandles({"NSE:COFORGE26OCT1500CE": [_candle(1, 70, 74)]}), day=DAY
    )

    assert "COFORGE" in text
    assert "+300" in text
    assert "target" in text
    assert "Gross:" in text


def test_summary_always_states_its_assumptions(repository: ShadowRepository) -> None:
    _store(repository)
    text = build_report(
        repository, FakeCandles({"NSE:COFORGE26OCT1500CE": [_candle(1, 70, 74)]}), day=DAY
    )

    # A P&L whose caveats are invisible is worse than no P&L.
    assert "Assumes entry filled at the signal's price" in text
    assert "gross of brokerage" in text


def test_summary_handles_a_day_with_no_signals(repository: ShadowRepository) -> None:
    text = build_report(repository, FakeCandles({}), day=DAY)

    assert "No signals today." in text


def test_summary_flags_unscored_trades(repository: ShadowRepository) -> None:
    _store(repository)
    text = build_report(
        repository, FakeCandles({}, failing="NSE:COFORGE26OCT1500CE"), day=DAY
    )

    assert "not scored" in text
    assert "Nothing could be scored today." in text


# --- The FYERS candle source --------------------------------------------------


class FakeHistoryClient:
    def __init__(self, response: object) -> None:
        self._response = response
        self.requests: list[dict] = []

    def history(self, data: dict) -> object:
        self.requests.append(data)
        return self._response


def test_fyers_candles_are_parsed() -> None:
    epoch = int(SIGNAL_TIME.timestamp())
    client = FakeHistoryClient(
        {"s": "ok", "candles": [[epoch, 70.0, 74.0, 69.0, 73.5, 1200]]}
    )

    candles = FyersCandleSource(client, tz=timezone.utc).candles("NSE:X", DAY)

    assert len(candles) == 1
    assert (candles[0].high, candles[0].low, candles[0].close) == (74.0, 69.0, 73.5)
    assert client.requests[0]["symbol"] == "NSE:X"
    assert client.requests[0]["resolution"] == "1"


def test_a_malformed_bar_does_not_lose_the_day() -> None:
    epoch = int(SIGNAL_TIME.timestamp())
    client = FakeHistoryClient(
        {"s": "ok", "candles": [["bad"], [epoch, 70.0, 74.0, 69.0, 73.5, 1]]}
    )

    candles = FyersCandleSource(client).candles("NSE:X", DAY)

    assert len(candles) == 1


def test_a_history_error_raises_so_the_trade_is_unscored() -> None:
    client = FakeHistoryClient({"s": "error", "message": "invalid token"})

    with pytest.raises(RuntimeError, match="FYERS history failed"):
        FyersCandleSource(client).candles("NSE:X", DAY)
