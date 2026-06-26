"""Trade engine — the business-logic layer.

The :class:`TradeEngine` decides **whether a parsed signal should be traded**.
It is deliberately broker-agnostic: it never talks to Zerodha, FYERS, or any
broker, and it never places an order. Its sole output is a :class:`TradeDecision`
that a downstream broker adapter (Phase 4/5) can act on — or ignore.

Decisions are made by composing small, independent :class:`TradeRule` objects,
each enforcing one concern (duplicate, auto-trading toggle, trading hours, daily
limit, signal validity). Rules are config-driven and the rule set is injectable,
so future risk rules slot in without touching the engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from .config import Config
from .logging_config import get_logger
from .lot_size import ConfigLotSizeProvider, LotSizeProvider
from .parser import Signal
from .repository import SignalRepository

__all__ = [
    "EvaluationContext",
    "TradeDecision",
    "TradeEngine",
    "TradeRule",
]

logger = get_logger(__name__)

#: A clock: returns the current moment (tz-aware). Injectable for testing.
Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class TradeDecision:
    """The engine's verdict on a single signal.

    ``execute`` is the go/no-go flag; ``quantity`` is the size to trade (always
    ``0`` when ``execute`` is ``False``); ``reason`` is a human-readable
    explanation suitable for logs and notifications.
    """

    execute: bool
    quantity: int
    reason: str


@dataclass(frozen=True, slots=True)
class EvaluationContext:
    """Pre-computed facts a rule may need, gathered once per evaluation.

    Keeping the I/O-derived facts (current time, today's trade count, whether
    the signal is a duplicate) in an immutable context makes every rule a pure
    function of its inputs — trivial to unit test in isolation.
    """

    now: datetime
    trades_today: int
    is_duplicate: bool
    lot_size: int | None


@runtime_checkable
class TradeRule(Protocol):
    """One business rule. Returns a rejection reason, or ``None`` to allow."""

    name: str

    def check(self, signal: Signal, context: EvaluationContext) -> str | None:
        """Return a rejection reason if the rule blocks the trade, else ``None``."""
        ...


# --- Concrete rules -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SignalValidityRule:
    """Reject structurally implausible signals (defence in depth after parsing)."""

    name: str = "signal_validity"

    def check(self, signal: Signal, context: EvaluationContext) -> str | None:
        if any(p <= 0 for p in (signal.entry_price, signal.stop_loss, signal.target)):
            return "Invalid signal: prices must be positive"
        if not signal.stop_loss < signal.entry_price < signal.target:
            return "Invalid signal: expected stop_loss < entry < target"
        return None


@dataclass(frozen=True, slots=True)
class AutoTradingRule:
    """Block everything when auto-trading is switched off."""

    config: Config
    name: str = "auto_trading"

    def check(self, signal: Signal, context: EvaluationContext) -> str | None:
        if not self.config.auto_trading:
            return "Auto-trading disabled"
        return None


@dataclass(frozen=True, slots=True)
class DuplicateRule:
    """Block a signal already seen, unless duplicates are explicitly allowed."""

    config: Config
    name: str = "duplicate"

    def check(self, signal: Signal, context: EvaluationContext) -> str | None:
        if context.is_duplicate and not self.config.allow_duplicates:
            return "Duplicate signal"
        return None


@dataclass(frozen=True, slots=True)
class TradingHoursRule:
    """Block trades outside configured market hours / on weekends."""

    config: Config
    name: str = "trading_hours"

    def check(self, signal: Signal, context: EvaluationContext) -> str | None:
        now = context.now
        if now.weekday() >= 5:  # Saturday/Sunday
            return "Market closed (weekend)"
        if not self.config.market_open <= now.time() <= self.config.market_close:
            return (
                f"Market closed (outside {self.config.market_open:%H:%M}"
                f"-{self.config.market_close:%H:%M})"
            )
        return None


@dataclass(frozen=True, slots=True)
class MaxTradesPerDayRule:
    """Block once the configured daily trade cap is reached."""

    config: Config
    name: str = "max_trades_per_day"

    def check(self, signal: Signal, context: EvaluationContext) -> str | None:
        if context.trades_today >= self.config.max_trades_per_day:
            return f"Daily trade limit reached ({self.config.max_trades_per_day})"
        return None


@dataclass(frozen=True, slots=True)
class LotSizeRule:
    """Block a signal whose underlying has no known lot size (can't be sized).

    Checked last, so it only fires for an otherwise-acceptable signal — i.e.
    when we actually need a lot size to compute the order quantity.
    """

    name: str = "lot_size"

    def check(self, signal: Signal, context: EvaluationContext) -> str | None:
        if context.lot_size is None:
            return f"No lot size configured for {signal.underlying}"
        return None


# --- Engine -------------------------------------------------------------------


class TradeEngine:
    """Decides whether a parsed signal should be traded.

    Dependencies (config, the signal repository used for duplicate/limit checks,
    the clock, and the rule set) are all injected — no globals, no broker. The
    engine owns ordering and orchestration; the rules own the actual policy.
    """

    def __init__(
        self,
        config: Config,
        repository: SignalRepository,
        *,
        lot_size_provider: LotSizeProvider | None = None,
        rules: tuple[TradeRule, ...] | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._config = config
        self._repository = repository
        # Config-backed by default; inject a BrokerLotSizeProvider here once the
        # broker is wired in (Phase 5) — see teletrader.lot_size for the seam.
        self._lot_sizes = lot_size_provider or ConfigLotSizeProvider(config.lot_sizes)
        self._clock = clock or self._default_clock
        self._rules = rules if rules is not None else self._build_rules()

    def evaluate(self, signal: Signal | None) -> TradeDecision:
        """Return a :class:`TradeDecision` for ``signal``.

        A ``None`` signal (parser found no valid signal) is rejected as
        malformed. Otherwise every rule is checked in order and the first
        rejection wins; if all pass, the trade is accepted.
        """
        if signal is None:
            return self._reject("Malformed signal: nothing to evaluate")

        context = self._build_context(signal)
        for rule in self._rules:
            reason = rule.check(signal, context)
            if reason is not None:
                logger.info("Signal rejected by %s rule: %s", rule.name, reason)
                return self._reject(reason)
        return self._accept(signal, context)

    def _build_rules(self) -> tuple[TradeRule, ...]:
        """Assemble the default, config-driven rule set (evaluation order)."""
        return (
            SignalValidityRule(),
            AutoTradingRule(self._config),
            TradingHoursRule(self._config),
            DuplicateRule(self._config),
            MaxTradesPerDayRule(self._config),
            LotSizeRule(),
        )

    def _build_context(self, signal: Signal) -> EvaluationContext:
        """Gather the facts the rules need (single point of I/O)."""
        now = self._clock()
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return EvaluationContext(
            now=now,
            trades_today=self._repository.count_since(start_of_day),
            is_duplicate=self._repository.exists(signal),
            lot_size=self._lot_sizes.lot_size_for(signal.underlying),
        )

    def _accept(self, signal: Signal, context: EvaluationContext) -> TradeDecision:
        # LotSizeRule has already guaranteed a known lot size by this point.
        lot_size = context.lot_size
        assert lot_size is not None  # narrows type; enforced by LotSizeRule
        quantity = self._config.trade_lots * lot_size
        decision = TradeDecision(execute=True, quantity=quantity, reason="Signal accepted")
        logger.info(
            "Signal accepted: %s (%s lot(s) x %s = quantity %s)",
            signal,
            self._config.trade_lots,
            lot_size,
            quantity,
        )
        return decision

    def _reject(self, reason: str) -> TradeDecision:
        return TradeDecision(execute=False, quantity=0, reason=reason)

    def _default_clock(self) -> datetime:
        """Current time in the configured market timezone."""
        return datetime.now(ZoneInfo(self._config.market_timezone))
