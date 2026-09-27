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

from .channel2 import looks_like_signal, parse_channel2_signal
from .commands import ManagementCommand, parse_message
from .execution import (
    ExecutionResult,
    Executor,
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
    SHADOWED = "shadowed"        # stored, accepted, and costed against the broker
                                 #   without submitting (shadow mode)
    MISSED = "missed"            # looked like a signal but could not be parsed
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
    #: The original message text — carried only for a MISSED result, where the
    #: unparsed text is the whole point of the report.
    raw_text: str | None = None


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
    """Channel 2's pipeline: *parse → store → evaluate → shadow-execute*.

    Channel 2 is the channel being promoted to live trading, and this is the
    ramp-up shape of that path. It runs the same stages as
    :class:`SignalPipeline` — the engine's gates apply, so a signal outside market
    hours or over the daily cap is declined exactly as it would be live — but the
    executor it is given is normally the **shadow** one, which builds the real
    broker order and stops before submitting. The trade state machine and
    management commands are deliberately not wired up yet: with no order actually
    placed there is nothing for them to manage, and pretending otherwise would
    make the shadow reports less trustworthy, not more.

    Passing no ``engine``/``executor`` keeps the original parse-only behaviour.

    A message that fails to parse but *looks* like an entry is reported as
    :attr:`PipelineStatus.MISSED` rather than silently ignored — this channel's
    formatting is loose, and a missed trade must be visible.
    """

    def __init__(
        self,
        repository: SignalRepository,
        engine: TradeEngine | None = None,
        executor: Executor | None = None,
    ) -> None:
        self._repository = repository
        self._engine = engine
        self._executor = executor

    def process(
        self, text: str | None, *, when: datetime | None = None
    ) -> PipelineResult:
        """Parse one channel-2 message and store, evaluate and cost it."""
        signal = parse_channel2_signal(text)
        if signal is None:
            if looks_like_signal(text):
                logger.warning(
                    "Channel-2 message looks like a signal but did not parse: %r", text
                )
                return PipelineResult(PipelineStatus.MISSED, raw_text=text)
            return PipelineResult(PipelineStatus.IGNORED)

        # Evaluate before storing, so the engine's duplicate/daily-count checks
        # see only prior history (same ordering as the channel-1 pipeline).
        decision = self._engine.evaluate(signal) if self._engine else None

        try:
            stored = self._repository.add(signal, created_at=when)
        except DuplicateSignalError:
            return PipelineResult(PipelineStatus.DUPLICATE, signal=signal)

        if decision is None or self._executor is None:
            return PipelineResult(
                PipelineStatus.STORED, signal=signal, stored_id=stored.id
            )

        if not decision.execute:
            logger.info(
                "Channel-2 signal #%s not traded: %s", stored.id, decision.reason
            )
            return PipelineResult(
                PipelineStatus.NOT_TRADED,
                signal=signal,
                stored_id=stored.id,
                decision=decision,
            )

        order = build_order_request(signal, decision, signal_id=stored.id)
        execution = self._executor.execute(order)
        # SHADOWED is reported only when the executor really did withhold the
        # order; a channel switched to a live broker reports EXECUTED, so the
        # status never overstates or understates what happened to the money.
        status = (
            PipelineStatus.SHADOWED
            if execution.shadow is not None
            else PipelineStatus.EXECUTED
        )
        return PipelineResult(
            status,
            signal=signal,
            stored_id=stored.id,
            decision=decision,
            execution=execution,
        )
