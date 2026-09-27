"""End-of-day scoring: what the day's shadowed trades would have been worth.

Shadow mode answers "would this order have been accepted?". This module answers
the question that actually decides whether the channel is worth trading: *would
following it have made money?* It replays each shadowed trade against the
contract's own intraday candles — if the price reached the stop, the trade is
stopped; if it reached the target, the trade is taken; if neither, it is marked
to the close — and totals the result.

The numbers rest on three assumptions, and they are stated in the report itself
rather than buried here, because a P&L whose caveats are invisible is worse than
no P&L at all:

* **The entry is assumed filled at the signal's stated price.** Real market
  orders slip, and thin stock options slip most.
* **When one candle's range covers both the stop and the target**, the stop is
  assumed to have come first. Minute candles cannot say which came first, and
  the pessimistic reading is the honest one.
* **The figure is gross.** Brokerage, STT and exchange charges are not modelled,
  and on single-lot option trades they are not negligible.

Market data comes from the broker's own history endpoint, so the prices are the
ones that contract actually traded at — not a proxy or an index.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone, tzinfo
from typing import Any, Protocol, Sequence

from .logging_config import get_logger
from .shadow_repository import Outcome, ShadowRepository, StoredShadowRun

__all__ = [
    "Candle",
    "CandleSource",
    "DailyReport",
    "FyersCandleSource",
    "ScoredTrade",
    "build_report",
    "format_report",
    "score_day",
    "score_run",
]

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Candle:
    """One intraday bar of the option contract."""

    start: datetime
    open: float
    high: float
    low: float
    close: float


class CandleSource(Protocol):
    """Supplies a contract's intraday candles for a trading day."""

    def candles(self, tradingsymbol: str, day: date) -> list[Candle]:
        """Return ``tradingsymbol``'s candles for ``day``, oldest first."""
        ...


@dataclass(frozen=True, slots=True)
class ScoredTrade:
    """One shadowed trade, priced."""

    run: StoredShadowRun
    outcome: Outcome
    exit_price: float | None
    pnl: float | None
    note: str = ""


@dataclass(frozen=True, slots=True)
class DailyReport:
    """Everything the end-of-day summary needs."""

    day: date
    source: str
    trades: tuple[ScoredTrade, ...]

    @property
    def scored(self) -> tuple[ScoredTrade, ...]:
        """Trades whose outcome market data could actually settle."""
        return tuple(t for t in self.trades if t.pnl is not None)

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl or 0.0 for t in self.scored)

    @property
    def deployed(self) -> float:
        """Capital that would have been committed, at the assumed entries."""
        return sum(
            (t.run.run.entry_price or 0.0) * t.run.run.quantity for t in self.scored
        )

    def count(self, outcome: Outcome) -> int:
        return sum(1 for t in self.trades if t.outcome is outcome)


def score_run(run: StoredShadowRun, candles: Sequence[Candle]) -> ScoredTrade:
    """Replay one shadowed trade against its contract's candles.

    Walks forward from the moment the signal arrived. The stop is checked before
    the target within each bar: a minute candle records only a range, not the
    order in which its extremes occurred, so a bar that spans both is read the
    pessimistic way rather than the flattering one.
    """
    detail = run.run
    if not detail.accepted:
        return ScoredTrade(
            run, Outcome.UNKNOWN, None, None, "Order would not have been accepted."
        )
    entry = detail.entry_price
    if entry is None:
        return ScoredTrade(run, Outcome.UNKNOWN, None, None, "No entry price.")

    after = [c for c in candles if c.start >= run.created_at]
    if not after:
        return ScoredTrade(
            run, Outcome.UNKNOWN, None, None, "No market data after the signal."
        )

    for candle in after:
        if detail.stop_loss is not None and candle.low <= detail.stop_loss:
            return _settle(run, detail.stop_loss, Outcome.STOPPED, entry)
        if detail.target is not None and candle.high >= detail.target:
            return _settle(run, detail.target, Outcome.TARGET, entry)

    close = after[-1].close
    return _settle(run, close, Outcome.OPEN, entry, note="Neither hit; marked to close.")


def score_day(
    repository: ShadowRepository,
    source_data: CandleSource,
    *,
    day: date,
    source: str = "channel2",
    persist: bool = True,
) -> DailyReport:
    """Score every shadow run recorded on ``day`` and return the report.

    A contract whose candles cannot be fetched is reported as ``UNKNOWN`` rather
    than dropped or guessed at, so the summary's trade count always matches what
    actually happened.
    """
    runs = repository.for_day(day, source=source)
    trades: list[ScoredTrade] = []
    candle_cache: dict[str, list[Candle]] = {}

    for run in runs:
        symbol = run.run.tradingsymbol
        if symbol not in candle_cache:
            try:
                candle_cache[symbol] = source_data.candles(symbol, day)
            except Exception as exc:  # noqa: BLE001 — a report must still be sent
                logger.warning("Could not fetch candles for %s: %s", symbol, exc)
                candle_cache[symbol] = []
        scored = score_run(run, candle_cache[symbol])
        trades.append(scored)
        if persist and scored.pnl is not None:
            repository.score(
                run.id,
                outcome=scored.outcome,
                exit_price=scored.exit_price,
                pnl=scored.pnl,
            )

    return DailyReport(day=day, source=source, trades=tuple(trades))


def format_report(report: DailyReport) -> str:
    """Render the day's report as the Telegram summary."""
    lines = [
        f"📊 {report.source} — {report.day.strftime('%d %b %Y')} (shadow)",
    ]
    if not report.trades:
        lines.append("No signals today.")
        return "\n".join(lines)

    lines.append(
        f"{len(report.trades)} signal(s) · {report.count(Outcome.TARGET)} target · "
        f"{report.count(Outcome.STOPPED)} stopped · {report.count(Outcome.OPEN)} open"
    )
    lines.append("")

    for trade in report.trades:
        detail = trade.run.run
        name = detail.underlying or detail.tradingsymbol
        if trade.pnl is None:
            lines.append(f"  ? {name} — not scored ({trade.note})")
            continue
        mark = {"target": "✅", "stopped": "❌", "open": "⏳"}.get(
            trade.outcome.value, "?"
        )
        lines.append(
            f"  {mark} {name} {_money(trade.pnl)}  "
            f"({_price(detail.entry_price)} → {_price(trade.exit_price)}, "
            f"{trade.outcome.value})"
        )

    lines.append("")
    if report.scored:
        lines.append(f"Gross: {_money(report.total_pnl)} on {_price(report.deployed)} deployed")
    else:
        lines.append("Nothing could be scored today.")

    unscored = len(report.trades) - len(report.scored)
    if unscored:
        lines.append(f"({unscored} not scored — see notes above.)")

    # The assumptions travel with the number. A P&L whose caveats are invisible
    # is worse than no P&L.
    lines.append("")
    lines.append(
        "Assumes entry filled at the signal's price; a bar spanning both "
        "stop and target counts as stopped; gross of brokerage and taxes."
    )
    return "\n".join(lines)


def build_report(
    repository: ShadowRepository,
    source_data: CandleSource,
    *,
    day: date,
    source: str = "channel2",
) -> str:
    """Score the day and render the summary in one call."""
    return format_report(score_day(repository, source_data, day=day, source=source))


class FyersCandleSource:
    """Intraday candles from the FYERS history endpoint.

    The client is the same one the executor uses, so the prices come from the
    broker the trade would have been placed with. Needs a valid daily access
    token; without one the caller sees an exception per symbol and those trades
    are reported unscored rather than guessed at.
    """

    #: FYERS resolution code for one-minute bars.
    RESOLUTION = "1"

    def __init__(self, client: Any, *, tz: tzinfo = timezone.utc) -> None:
        self._client = client
        self._tz = tz

    def candles(self, tradingsymbol: str, day: date) -> list[Candle]:
        response = self._client.history(
            data={
                "symbol": tradingsymbol,
                "resolution": self.RESOLUTION,
                "date_format": "1",
                "range_from": day.isoformat(),
                "range_to": day.isoformat(),
                "cont_flag": "1",
            }
        )
        if not isinstance(response, dict) or response.get("s") == "error":
            raise RuntimeError(f"FYERS history failed: {response}")
        raw = response.get("candles") or []
        candles: list[Candle] = []
        for row in raw:
            try:
                start = datetime.fromtimestamp(int(row[0]), tz=timezone.utc)
                candles.append(
                    Candle(
                        start=start.astimezone(self._tz),
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                    )
                )
            except (IndexError, TypeError, ValueError):
                continue  # one malformed bar must not lose the whole day
        return candles


def _settle(
    run: StoredShadowRun,
    exit_price: float,
    outcome: Outcome,
    entry: float,
    *,
    note: str = "",
) -> ScoredTrade:
    """Price a long-option trade closed at ``exit_price``."""
    pnl = round((exit_price - entry) * run.run.quantity, 2)
    return ScoredTrade(run, outcome, exit_price, pnl, note)


def _money(value: float) -> str:
    """Render a P&L with an explicit sign and thousands separators."""
    return f"{value:+,.0f}"


def _price(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"
