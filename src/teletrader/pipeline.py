"""The signal-processing pipeline.

Glues the stages together for a single inbound message — *parse → store →
evaluate → execute* — and is deliberately free of any Telegram/Telethon
dependency so it can be unit-tested directly. The :class:`TelegramListener` owns
Telegram I/O and simply hands each message's text to :meth:`SignalPipeline.process`.

The mapping from a :class:`~teletrader.parser.Signal` (+ the engine's
:class:`~teletrader.trade_engine.TradeDecision`) to an
:class:`~teletrader.execution.OrderRequest` lives here — it is the seam between
the decision world and the execution world, so neither layer depends on the
other's types. Dependencies (repository, engine, executor) are injected.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .execution import (
    ExecutionResult,
    Executor,
    OrderRequest,
    OrderType,
    ProductType,
    TransactionType,
)
from .logging_config import get_logger
from .parser import Action, Signal, parse_signal
from .repository import DuplicateSignalError, SignalRepository
from .trade_engine import TradeDecision, TradeEngine

__all__ = ["PipelineResult", "PipelineStatus", "SignalPipeline", "build_order_request"]

logger = get_logger(__name__)


class PipelineStatus(str, Enum):
    """What became of one inbound message."""

    IGNORED = "ignored"          # not a valid signal (noise)
    DUPLICATE = "duplicate"      # already stored on this trading day
    NOT_TRADED = "not_traded"    # stored, but the engine declined to trade
    EXECUTED = "executed"        # stored, accepted, and sent to the executor


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """The outcome of processing one message, for the caller to present/log."""

    status: PipelineStatus
    signal: Signal | None = None
    stored_id: int | None = None
    decision: TradeDecision | None = None
    execution: ExecutionResult | None = None


def build_order_request(
    signal: Signal, decision: TradeDecision, *, signal_id: int | None = None
) -> OrderRequest:
    """Map a signal + its trade decision to a broker-independent order request.

    The ``symbol`` is a readable composition of the option's fields
    (e.g. ``"NIFTY 23900 PE"``) for logs and the dry run; the structured
    ``underlying``/``strike``/``option_type`` fields travel alongside it so the
    live :class:`~teletrader.execution.kite.KiteExecutor` can resolve the broker's
    exact tradingsymbol/expiry. The breakout entry is sent as a ``MARKET`` order,
    with the signal's entry/stop/target carried along for the log and audit trail.
    """
    symbol = f"{signal.underlying} {signal.strike} {signal.option_type.value}"
    transaction_type = (
        TransactionType.BUY if signal.action is Action.BUY else TransactionType.SELL
    )
    return OrderRequest(
        symbol=symbol,
        transaction_type=transaction_type,
        quantity=decision.quantity,
        order_type=OrderType.MARKET,
        product=ProductType.INTRADAY,
        exchange="NFO",
        entry_price=signal.entry_price,
        stop_loss=signal.stop_loss,
        target=signal.target,
        signal_id=signal_id,
        underlying=signal.underlying,
        strike=signal.strike,
        option_type=signal.option_type.value,
    )


class SignalPipeline:
    """Runs one message through parse → store → evaluate → execute.

    Each dependency is injected (DI): the :class:`SignalRepository` (persistence
    + dedupe), the :class:`TradeEngine` (the trade/no-trade decision), and the
    :class:`Executor` (order submission — a dry run today). The pipeline performs
    no Telegram I/O.
    """

    def __init__(
        self,
        repository: SignalRepository,
        engine: TradeEngine,
        executor: Executor,
    ) -> None:
        self._repository = repository
        self._engine = engine
        self._executor = executor

    def process(self, text: str | None, *, when: datetime | None = None) -> PipelineResult:
        """Process one message's ``text`` and return what happened.

        ``when`` (the message timestamp, tz-aware) is used as the stored signal's
        ``created_at`` so the per-day dedupe reflects when the signal arrived; it
        defaults to now.

        The engine evaluates **before** the signal is stored, so its duplicate /
        daily-count checks see only *prior* history, not the signal currently
        being processed. Storage is the authoritative same-day dedupe: a repeat
        is rejected by ``repository.add`` and reported as a duplicate.
        """
        signal = parse_signal(text)
        if signal is None:
            return PipelineResult(PipelineStatus.IGNORED)

        decision = self._engine.evaluate(signal)

        try:
            stored = self._repository.add(signal, created_at=when)
        except DuplicateSignalError:
            return PipelineResult(PipelineStatus.DUPLICATE, signal=signal)

        if not decision.execute:
            logger.info("Signal #%s not traded: %s", stored.id, decision.reason)
            return PipelineResult(
                PipelineStatus.NOT_TRADED,
                signal=signal,
                stored_id=stored.id,
                decision=decision,
            )

        order = build_order_request(signal, decision, signal_id=stored.id)
        execution = self._executor.execute(order)
        return PipelineResult(
            PipelineStatus.EXECUTED,
            signal=signal,
            stored_id=stored.id,
            decision=decision,
            execution=execution,
        )
