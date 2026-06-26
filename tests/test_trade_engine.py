"""Unit tests for the trade engine (business-logic layer).

The engine never touches a broker; these tests assert only its decisions. A
fixed clock and an in-memory repository keep every case deterministic.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from teletrader.config import Config
from teletrader.database import connect, initialize
from teletrader.parser import Action, OptionType, Signal
from teletrader.repository import SignalRepository
from teletrader.trade_engine import TradeEngine

IST = ZoneInfo("Asia/Kolkata")
# A Friday, 10:00 IST — a weekday inside the default 09:15–15:30 window.
TRADING_MOMENT = datetime(2026, 6, 26, 10, 0, tzinfo=IST)


# --- Fixtures / builders ------------------------------------------------------

@pytest.fixture
def connection() -> sqlite3.Connection:
    conn = connect(":memory:")
    initialize(conn)
    yield conn
    conn.close()


@pytest.fixture
def repo(connection: sqlite3.Connection) -> SignalRepository:
    return SignalRepository(connection)


def _config(**overrides: object) -> Config:
    """Build a Config with trade-engine defaults suited to tests."""
    defaults = dict(
        api_id=1,
        api_hash="hash",
        phone="+10000000000",
        session_name="test",
        channel="me",
        log_level="INFO",
        database_path=":memory:",
        auto_trading=True,
        allow_duplicates=False,
        max_trades_per_day=100,
        trade_quantity=15,
        market_open=time(9, 15),
        market_close=time(15, 30),
        market_timezone="Asia/Kolkata",
    )
    defaults.update(overrides)
    return Config(**defaults)  # type: ignore[arg-type]


def _signal(**overrides: object) -> Signal:
    defaults = dict(
        underlying="NIFTY",
        strike=23900,
        option_type=OptionType.PUT,
        action=Action.BUY,
        entry_price=165.0,
        stop_loss=150.0,
        target=198.0,
        target_open_ended=True,
        raw_text="NIFTY 23900 PE ABOVE 165\n\nSL-150\n\nTGT-200+",
    )
    defaults.update(overrides)
    return Signal(**defaults)  # type: ignore[arg-type]


def _engine(
    repo: SignalRepository,
    config: Config | None = None,
    *,
    now: datetime = TRADING_MOMENT,
) -> TradeEngine:
    return TradeEngine(config or _config(), repo, clock=lambda: now)


# --- Valid signal -------------------------------------------------------------

def test_valid_signal_is_accepted(repo: SignalRepository) -> None:
    decision = _engine(repo).evaluate(_signal())
    assert decision.execute is True
    assert decision.quantity == 15
    assert decision.reason == "Signal accepted"


def test_accepted_quantity_comes_from_config(repo: SignalRepository) -> None:
    decision = _engine(repo, _config(trade_quantity=50)).evaluate(_signal())
    assert decision.execute is True
    assert decision.quantity == 50


# --- Malformed signal ---------------------------------------------------------

def test_malformed_signal_is_rejected(repo: SignalRepository) -> None:
    decision = _engine(repo).evaluate(None)
    assert decision.execute is False
    assert decision.quantity == 0
    assert "Malformed" in decision.reason


def test_invalid_prices_are_rejected(repo: SignalRepository) -> None:
    # stop_loss above entry violates stop < entry < target ordering.
    decision = _engine(repo).evaluate(_signal(stop_loss=200.0))
    assert decision.execute is False
    assert "Invalid signal" in decision.reason


# --- Auto-trading disabled ----------------------------------------------------

def test_auto_trading_disabled_blocks_trade(repo: SignalRepository) -> None:
    decision = _engine(repo, _config(auto_trading=False)).evaluate(_signal())
    assert decision.execute is False
    assert decision.reason == "Auto-trading disabled"


# --- Duplicate signal ---------------------------------------------------------

def test_duplicate_signal_is_rejected(repo: SignalRepository) -> None:
    repo.add(_signal(), created_at=TRADING_MOMENT)
    decision = _engine(repo).evaluate(_signal())
    assert decision.execute is False
    assert decision.reason == "Duplicate signal"


def test_duplicate_allowed_when_configured(repo: SignalRepository) -> None:
    repo.add(_signal(), created_at=TRADING_MOMENT)
    decision = _engine(repo, _config(allow_duplicates=True)).evaluate(_signal())
    assert decision.execute is True


# --- Market closed ------------------------------------------------------------

def test_market_closed_before_open(repo: SignalRepository) -> None:
    before_open = datetime(2026, 6, 26, 8, 0, tzinfo=IST)
    decision = _engine(repo, now=before_open).evaluate(_signal())
    assert decision.execute is False
    assert "Market closed" in decision.reason


def test_market_closed_on_weekend(repo: SignalRepository) -> None:
    saturday = datetime(2026, 6, 27, 10, 0, tzinfo=IST)
    decision = _engine(repo, now=saturday).evaluate(_signal())
    assert decision.execute is False
    assert decision.reason == "Market closed (weekend)"


# --- Daily trade limit --------------------------------------------------------

def test_daily_trade_limit_exceeded(repo: SignalRepository) -> None:
    config = _config(max_trades_per_day=2)
    repo.add(_signal(strike=23900), created_at=TRADING_MOMENT)
    repo.add(_signal(strike=24000), created_at=TRADING_MOMENT)
    decision = _engine(repo, config).evaluate(_signal(strike=24100))
    assert decision.execute is False
    assert decision.reason == "Daily trade limit reached (2)"


def test_yesterdays_trades_do_not_count(repo: SignalRepository) -> None:
    config = _config(max_trades_per_day=1)
    yesterday = datetime(2026, 6, 25, 10, 0, tzinfo=IST)
    repo.add(_signal(strike=24000), created_at=yesterday)
    decision = _engine(repo, config).evaluate(_signal(strike=24100))
    assert decision.execute is True


# --- Rule ordering / injection ------------------------------------------------

def test_custom_rule_set_can_be_injected(repo: SignalRepository) -> None:
    # An empty rule set accepts everything, even with auto-trading off.
    engine = TradeEngine(
        _config(auto_trading=False), repo, rules=(), clock=lambda: TRADING_MOMENT
    )
    assert engine.evaluate(_signal()).execute is True
