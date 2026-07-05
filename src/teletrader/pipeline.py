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
from typing import Protocol

from .channel2 import parse_channel2_signal
from .commands import ManagementCommand, parse_message
from .execution import (
    ExecutionResult,
    OrderRequest,
    OrderType,
    ProductType,
    TransactionType,
)
from .logging_config import get_logger
from .parser import Action, Signal
from .repository import DuplicateSignalError, SignalRepository
from .trade_engine import TradeDecision, TradeEngine
from .trade_manager import ManagementOutcome, ManagementReport, TradeManager

__all__ = [
    "Channel2Pipeline",
    "MessageProcessor",
    "PipelineResult",
    "PipelineStatus",
    "SignalPipeline",
    "build_order_request",
]

logger = get_logger(__name__)


class PipelineStatus(str, Enum):
    """What became of one inbound message."""

    IGNORED = "ignored"          # not a valid signal or command (noise)
    DUPLICATE = "duplicate"      # already stored on this trading day
    NOT_TRADED = "not_traded"    # stored, but the engine declined to trade
    EXECUTED = "executed"        # stored, accepted, and sent to the executor
    STORED = "stored"            # parse-only channel: parsed + stored (not traded)
    # --- management-command outcomes ---
    NO_TARGET = "no_target"          # command, but no active trade to act on
    NOT_APPLICABLE = "not_applicable"  # command doesn't fit the trade's state
    CANCELLED = "cancelled"          # a pending entry was cancelled (avoid)
    EXITED = "exited"                # an open position was booked/exited
    MODIFIED = "modified"            # a stop-loss/target was moved
    MGMT_FAILED = "mgmt_failed"      # the management broker operation failed


#: Maps a TradeManager outcome to the pipeline status the caller sees.
_OUTCOME_STATUS: dict[ManagementOutcome, PipelineStatus] = {
    ManagementOutcome.NO_TARGET: PipelineStatus.NO_TARGET,
    ManagementOutcome.NOT_APPLICABLE: PipelineStatus.NOT_APPLICABLE,
    ManagementOutcome.CANCELLED: PipelineStatus.CANCELLED,
    ManagementOutcome.EXITED: PipelineStatus.EXITED,
    ManagementOutcome.MODIFIED: PipelineStatus.MODIFIED,
    ManagementOutcome.FAILED: PipelineStatus.MGMT_FAILED,
}


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """The outcome of processing one message, for the caller to present/log."""

    status: PipelineStatus
    signal: Signal | None = None
    stored_id: int | None = None
    decision: TradeDecision | None = None
    execution: ExecutionResult | None = None
    command: ManagementCommand | None = None
    management: ManagementReport | None = None


class MessageProcessor(Protocol):
    """One channel's message handler: text in, :class:`PipelineResult` out.

    Both the full trading :class:`SignalPipeline` and the parse-only
    :class:`Channel2Pipeline` satisfy this, so the :class:`TelegramListener` can
    route each channel to its own processor without knowing which is which.
    """

    def process(
        self, text: str | None, *, when: datetime | None = None
    ) -> PipelineResult: ...


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
        trade_manager: TradeManager,
    ) -> None:
        self._repository = repository
        self._engine = engine
        self._trade_manager = trade_manager

    def process(self, text: str | None, *, when: datetime | None = None) -> PipelineResult:
        """Process one message's ``text`` and return what happened.

        An inbound message is either an *entry signal* (stored, evaluated, and
        possibly executed) or a *management command* (applied to the active
        trade); anything else is ignored as noise.

        ``when`` (the message timestamp, tz-aware) is used as the stored signal's
        ``created_at`` so the per-day dedupe reflects when the signal arrived; it
        defaults to now.

        The engine evaluates **before** the signal is stored, so its duplicate /
        daily-count checks see only *prior* history, not the signal currently
        being processed. Storage is the authoritative same-day dedupe: a repeat
        is rejected by ``repository.add`` and reported as a duplicate.
        """
        # Sync open trades with the broker first (a stop/target may have filled),
        # so a command acts on fresh state. No-op for trades with no broker ids.
        self._trade_manager.reconcile()

        parsed = parse_message(text)
        if parsed is None:
            return PipelineResult(PipelineStatus.IGNORED)
        if isinstance(parsed, ManagementCommand):
            return self._process_command(parsed)

        return self._process_signal(parsed, when)

    def _process_command(self, command: ManagementCommand) -> PipelineResult:
        """Apply a management command to the active trade."""
        report = self._trade_manager.apply(command)
        return PipelineResult(
            _OUTCOME_STATUS[report.outcome],
            command=command,
            management=report,
        )

    def _process_signal(
        self, signal: Signal, when: datetime | None
    ) -> PipelineResult:
        """Store, evaluate, and (if accepted) execute an entry signal."""
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

        # The trade manager submits the entry, awaits its fill, and (on a fill)
        # places the resting protective orders — opening a trackable active trade
        # so later management commands have real broker orders to act on. A
        # rejected/failed entry opens nothing.
        order = build_order_request(signal, decision, signal_id=stored.id)
        execution = self._trade_manager.open_position(
            signal, order, signal_id=stored.id, when=when
        )
        return PipelineResult(
            PipelineStatus.EXECUTED,
            signal=signal,
            stored_id=stored.id,
            decision=decision,
            execution=execution,
        )


class Channel2Pipeline:
    """Parse-only pipeline for the second channel: *parse → store → log*.

    Channel 2 is not traded yet (see the project decisions), so unlike
    :class:`SignalPipeline` there is no engine, executor, or trade manager — a
    parsed signal is simply persisted (tagged with its channel via the injected
    source-scoped :class:`SignalRepository`) for the record and for future use.
    Same-day duplicates are rejected by the repository; anything that does not
    parse is ignored as noise.
    """

    def __init__(self, repository: SignalRepository) -> None:
        self._repository = repository

    def process(
        self, text: str | None, *, when: datetime | None = None
    ) -> PipelineResult:
        """Parse one channel-2 message and, if it is a signal, store it."""
        signal = parse_channel2_signal(text)
        if signal is None:
            return PipelineResult(PipelineStatus.IGNORED)

        try:
            stored = self._repository.add(signal, created_at=when)
        except DuplicateSignalError:
            return PipelineResult(PipelineStatus.DUPLICATE, signal=signal)

        return PipelineResult(
            PipelineStatus.STORED, signal=signal, stored_id=stored.id
        )
