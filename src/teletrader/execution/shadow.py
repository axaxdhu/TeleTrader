"""The FYERS shadow executor — the live path, stopped at the broker's door.

A dry run proves that *we* built a sane order; it never contacts a broker, so it
cannot tell you whether the broker would have taken it. The things that actually
sink a live stock-option order are all broker-side: the exact tradingsymbol, the
expiry that is currently listed, the exchange lot size, and whether the account
has the funds. :class:`FyersShadowExecutor` answers that sharper question — *would
this order have gone through?* — by walking the **identical** code path as
:class:`~teletrader.execution.fyers.FyersExecutor` (same validation, same symbol
resolution, same payload construction) and stopping immediately before
``place_order``.

It exists for the ramp-up period on a new channel: run it for weeks, read the
resulting alerts, and switch the channel's broker from ``fyers_shadow`` to
``fyers`` once the payloads are consistently right. Because it *is* the live path
minus the final call, "it looked fine in shadow" means something.

It places no order, so it has no broker order id, and the management operations
it inherits are never reached in practice (shadow mode runs without the trade
state machine). Nothing here can reach ``place_order``.
"""

from __future__ import annotations

import time
from datetime import timezone, tzinfo
from typing import Any

from ..logging_config import get_logger
from .fyers import Clock, FyersClient, FyersExecutor
from .kite_instruments import InstrumentResolver
from .kite_instruments import ResolvedInstrument
from .repository import ExecutionRepository
from .exceptions import AuthenticationError, InvalidOrderError
from .models import (
    ExecutionResult,
    ExecutionStatus,
    OrderRequest,
    OrderState,
    OrderStatus,
    OrderType,
    ShadowLeg,
    ShadowReport,
    TransactionType,
)
from .validation import validate_order

__all__ = ["FyersShadowExecutor"]

logger = get_logger(__name__)

#: The mode string this executor implements (a channel's broker setting).
MODE = "fyers_shadow"

#: FYERS reports balances as a list of labelled buckets; this is the one that
#: represents cash actually available to trade with.
_AVAILABLE_BALANCE_TITLE = "available balance"


class FyersShadowExecutor(FyersExecutor):
    """Builds the real FYERS order, checks the account, and does not send it.

    Inherits construction (client, resolver, repository, clock) from
    :class:`FyersExecutor` — including the real symbol master — so the only
    difference from live trading is the missing ``place_order`` call.
    """

    def __init__(
        self,
        repository: ExecutionRepository,
        *,
        app_id: str | None = None,
        access_token: str | None = None,
        client: FyersClient | None = None,
        resolver: InstrumentResolver | None = None,
        tz: tzinfo = timezone.utc,
        clock: Clock | None = None,
    ) -> None:
        """Build the executor, tolerating absent FYERS credentials.

        The live executor refuses to construct without a token, and rightly so —
        it places orders. Shadow mode places none, and the symbol master it
        resolves contracts from is a public file, so it runs without an account
        at all: the payload, symbol, expiry and lot size are all still real, and
        only the funds check is reported as unavailable. That makes shadow mode
        usable *before* the broker is set up, which is when it is most useful.
        """
        if client is None and not (app_id and access_token):
            logger.warning(
                "[SHADOW] No FYERS credentials: orders will still be built and "
                "resolved against the public symbol master, but the funds check "
                "cannot run."
            )
            client = _UnauthenticatedClient()
        super().__init__(
            repository,
            app_id=app_id,
            access_token=access_token,
            client=client,
            resolver=resolver,
            tz=tz,
            clock=clock,
        )

    @property
    def mode(self) -> str:
        return MODE

    def execute(self, order: OrderRequest) -> ExecutionResult:
        """Validate, resolve, price and cost the order — then stop.

        The outcome is ``SUCCESS`` when the order is complete and affordable
        (meaning: it *would* have been submitted), and ``REJECTED`` when
        validation, symbol resolution or the funds check says it would not have
        been. Either way a :class:`ShadowReport` is attached for the alert, and
        nothing is sent.
        """
        timestamp = self._clock()
        started = time.monotonic()

        prepared = self._prepare(order, timestamp=timestamp, started=started)
        if isinstance(prepared, ExecutionResult):
            # Validation or symbol resolution already failed: the order would not
            # have reached FYERS at all, and that is the finding worth reporting.
            return prepared
        instrument, params = prepared

        report = self._build_report(order, instrument, params)
        if report.funds_ok is False:
            result = ExecutionResult(
                status=ExecutionStatus.REJECTED,
                order=order,
                remarks=(
                    f"Would NOT go through: {report.funds_note} "
                    f"({instrument.tradingsymbol})."
                ),
                timestamp=timestamp,
                shadow=report,
            )
        else:
            result = ExecutionResult(
                status=ExecutionStatus.SUCCESS,
                order=order,
                remarks=(
                    f"[SHADOW] Would submit to FYERS: {instrument.tradingsymbol} "
                    f"(expiry {instrument.expiry.isoformat()}), qty {report.quantity} "
                    f"= {report.lots} lot x {report.lot_size}. Nothing was sent."
                ),
                timestamp=timestamp,
                shadow=report,
            )

        self._log(result, report)
        # The payload goes to the log line (the executions table stores the
        # verdict, not the raw request); the resolved contract, expiry and
        # quantity travel in ``remarks``, which *is* persisted, so the stored
        # history alone still says which contract would have been bought.
        return self._finalize(
            result,
            started,
            broker_response=str(params),
            tradingsymbol=instrument.tradingsymbol,
        )

    def get_order_state(self, broker_order_id: str | None) -> OrderState:
        """No order exists to poll — shadow mode submits nothing."""
        return OrderState(OrderStatus.UNKNOWN, raw="shadow")

    # --- Report construction --------------------------------------------------

    def _build_report(
        self,
        order: OrderRequest,
        instrument: ResolvedInstrument,
        params: dict[str, Any],
    ) -> ShadowReport:
        """Assemble what the user needs to judge whether the order is right."""
        lot_size = instrument.lot_size
        # Quantity is decided upstream (lots x lot size); derive the lot count back
        # out for display, and report 0 rather than dividing by a bad lot size.
        lots = order.quantity // lot_size if lot_size else 0
        required, available, ok, note = self._check_funds(order)
        return ShadowReport(
            protective=self._build_protective_legs(order, instrument, lots, lot_size),
            tradingsymbol=instrument.tradingsymbol,
            exchange=instrument.exchange,
            expiry=instrument.expiry,
            lot_size=lot_size,
            lots=lots,
            quantity=order.quantity,
            payload=dict(params),
            funds_required=required,
            funds_available=available,
            funds_ok=ok,
            funds_note=note,
        )

    def _build_protective_legs(
        self,
        order: OrderRequest,
        instrument: ResolvedInstrument,
        lots: int,
        lot_size: int,
    ) -> tuple[ShadowLeg, ...]:
        """Build the resting stop-loss and target exits the entry would get.

        These mirror what the live path places after a fill: a SELL **SL-M** at
        the signal's stop and a SELL **LIMIT** at its target, both flattening the
        long option. They are built and validated here so a shadow run reports
        the whole position rather than only the way into it — an entry that fills
        and then cannot be protected is not a trade anyone wants.

        The live path sizes these to the *actual* fill; with nothing filled there
        is no fill to size to, so the requested quantity is used and the report
        says so rather than implying otherwise.
        """
        legs: list[ShadowLeg] = []
        for kind, order_type, price in (
            ("stop-loss", OrderType.SL_M, order.stop_loss),
            ("target", OrderType.LIMIT, order.target),
        ):
            if price is None:
                legs.append(
                    ShadowLeg(
                        kind=kind,
                        order_type=order_type.value,
                        price=None,
                        note=f"No {kind} in the signal — this leg would not be placed.",
                    )
                )
                continue
            exit_order = self._protective_order(order, order_type, price)
            try:
                validate_order(exit_order)
            except InvalidOrderError as exc:
                legs.append(
                    ShadowLeg(
                        kind=kind,
                        order_type=order_type.value,
                        price=price,
                        accepted=False,
                        note=f"Would be rejected: {exc}",
                    )
                )
                continue
            legs.append(
                ShadowLeg(
                    kind=kind,
                    order_type=order_type.value,
                    price=price,
                    payload=dict(self._to_fyers_params(exit_order, instrument)),
                    accepted=True,
                    note=f"Would rest at {_fmt(price)} for {order.quantity} qty.",
                )
            )
        return tuple(legs)

    @staticmethod
    def _protective_order(
        order: OrderRequest, order_type: OrderType, price: float
    ) -> OrderRequest:
        """A resting SELL exit flattening the long-option entry.

        Mirrors ``TradeManager._protective_order``: for a target the limit price
        travels in ``entry_price`` (the field doubles as the order's price), and
        for a stop it is the ``trigger_price``.
        """
        return OrderRequest(
            symbol=order.symbol,
            transaction_type=TransactionType.SELL,
            quantity=order.quantity,
            order_type=order_type,
            product=order.product,
            exchange=order.exchange,
            entry_price=price if order_type is OrderType.LIMIT else None,
            trigger_price=price if order_type is OrderType.SL_M else None,
            signal_id=order.signal_id,
            underlying=order.underlying,
            strike=order.strike,
            option_type=order.option_type,
        )

    def _check_funds(
        self, order: OrderRequest
    ) -> tuple[float | None, float | None, bool | None, str]:
        """Compare the cash this order needs against the account's balance.

        Buying an option costs the full premium (``entry price x quantity``), so
        for a BUY the requirement is exact. A SELL is margined by span/exposure
        rules this app does not model, so the check is skipped rather than
        guessed at. Any failure to read the balance yields ``None`` (unknown) —
        never a silent pass.
        """
        if order.transaction_type is not TransactionType.BUY:
            return None, None, None, "Funds check skipped (not a buy)."

        price = order.entry_price
        if price is None:
            return None, None, None, "Funds check skipped (no entry price)."
        required = round(price * order.quantity, 2)

        available = self._available_balance()
        if available is None:
            return required, None, None, "Funds check unavailable (balance not read)."

        if available >= required:
            return required, available, True, "Sufficient funds."
        return (
            required,
            available,
            False,
            f"insufficient funds - needs {required:.2f}, available {available:.2f}",
        )

    def _available_balance(self) -> float | None:
        """Read the FYERS available balance, or ``None`` if it cannot be read.

        The funds endpoint is optional on the client Protocol (tests inject fakes
        without it), and a missing or malformed response must degrade to "unknown"
        rather than crash a shadow run — its job is to report, never to fail.
        """
        funds = getattr(self._fyers, "funds", None)
        if not callable(funds):
            return None
        try:
            response = funds()
        except Exception as exc:  # noqa: BLE001 — a shadow run must never crash
            logger.warning("[SHADOW] Could not read FYERS funds: %s", exc)
            return None
        return _available_from_funds(response)

    @staticmethod
    def _log(result: ExecutionResult, report: ShadowReport) -> None:
        """Emit the human-readable ``[SHADOW]`` block of the would-be order."""
        order = result.order
        logger.info(
            "[SHADOW] NOT SENT — this is what would have gone to FYERS:\n"
            "Symbol: %s (%s)\n"
            "Expiry: %s\n"
            "Side: %s\n"
            "Quantity: %s (%s lot x %s)\n"
            "Order Type: %s\n"
            "Entry: %s\n"
            "Stop Loss: %s\n"
            "Target: %s\n"
            "Funds: %s\n"
            "Protection: %s\n"
            "Payload: %s\n"
            "Verdict: %s",
            report.tradingsymbol,
            report.exchange,
            report.expiry.isoformat(),
            order.transaction_type.value,
            report.quantity,
            report.lots,
            report.lot_size,
            order.order_type.value,
            _fmt(order.entry_price),
            _fmt(order.stop_loss),
            _fmt(order.target),
            report.funds_note,
            " | ".join(f"{leg.kind}: {leg.note}" for leg in report.protective) or "-",
            report.payload,
            result.remarks,
        )


class _UnauthenticatedClient:
    """Stands in for the FYERS client when there are no credentials.

    It deliberately offers no ``funds`` method, so the balance reads as unknown
    rather than as a pass, and every trading call raises — nothing here can reach
    a broker even by accident.
    """

    def _refuse(self, what: str) -> ExecutionResult:
        raise AuthenticationError(
            f"Shadow mode has no FYERS credentials and never {what} anyway."
        )

    def place_order(self, data: dict[str, Any]) -> dict[str, Any]:
        self._refuse("places orders")

    def modify_order(self, data: dict[str, Any]) -> dict[str, Any]:
        self._refuse("modifies orders")

    def cancel_order(self, data: dict[str, Any]) -> dict[str, Any]:
        self._refuse("cancels orders")

    def orderbook(self, data: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"s": "ok", "orderBook": []}


def _available_from_funds(response: Any) -> float | None:
    """Pull the available balance out of a FYERS ``funds()`` response.

    FYERS returns ``{"s": "ok", "fund_limit": [{"title": ..., "equityAmount": ...}]}``.
    The labelled bucket is matched by title; anything unexpected reads as unknown.
    """
    if not isinstance(response, dict) or response.get("s") == "error":
        return None
    buckets = response.get("fund_limit")
    if not isinstance(buckets, list):
        return None
    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        title = str(bucket.get("title", "")).strip().lower()
        if title != _AVAILABLE_BALANCE_TITLE:
            continue
        amount = bucket.get("equityAmount", bucket.get("equity_amount"))
        try:
            return float(amount)
        except (TypeError, ValueError):
            return None
    return None


def _fmt(value: float | None) -> str:
    """Render an optional price: ``-`` when absent, no trailing ``.0`` otherwise."""
    if value is None:
        return "-"
    return str(int(value)) if float(value).is_integer() else str(value)
