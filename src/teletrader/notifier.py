"""Push notifications for recognised signals and their outcomes.

The ``Notifications`` box in the project architecture: after the pipeline processes
a message, an optional :class:`Notifier` sends you a short alert via a Telegram
**bot**. A bot is used (rather than messaging your own account) because Telegram
push-notifies you for a bot's messages but not for your own — so a bot is what
actually makes your phone buzz.

Kept deliberately simple and decoupled:

* Delivery is a single HTTPS POST to the Bot API (no extra dependency; stdlib
  ``urllib``). The sender is injectable so tests never hit the network.
* It is **best-effort**: any failure is logged and swallowed, never raised — a
  notification problem must not disturb signal processing.
* :func:`format_alert` turns a :class:`~teletrader.pipeline.PipelineResult` into the
  alert text (or ``None`` to skip pure noise), so the listener stays thin.
* Disabled by default: :func:`create_notifier` returns a no-op :class:`NullNotifier`
  unless notifications are configured, so nothing changes until you turn it on.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Callable, Protocol

from .config import Config
from .execution import ExecutionStatus
from .logging_config import get_logger
from .pipeline import PipelineResult, PipelineStatus

__all__ = [
    "NullNotifier",
    "Notifier",
    "TelegramBotNotifier",
    "create_notifier",
    "format_alert",
]

logger = get_logger(__name__)

#: How a message is delivered to the Bot API. Injectable for tests.
Sender = Callable[[str, str], None]


class Notifier(Protocol):
    """Sends a short alert somewhere the user will see it."""

    def notify(self, text: str) -> None:  # pragma: no cover - interface
        ...


class NullNotifier:
    """A no-op notifier used when notifications are disabled."""

    def notify(self, text: str) -> None:
        return None


class TelegramBotNotifier:
    """Sends alerts to a chat via a Telegram bot (best-effort, never raises).

    The bot token and target chat id are injected (from config); the low-level
    sender is injectable so tests exercise formatting/flow without any network.
    """

    _API = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(
        self, bot_token: str, chat_id: str, *, sender: Sender | None = None
    ) -> None:
        self._token = bot_token
        self._chat_id = chat_id
        self._send = sender or _http_post

    def notify(self, text: str) -> None:
        """Send ``text`` to the configured chat; log and swallow any failure."""
        url = self._API.format(token=self._token)
        payload = json.dumps({"chat_id": self._chat_id, "text": text})
        try:
            self._send(url, payload)
        except Exception as exc:  # noqa: BLE001 — a notify failure must not disturb trading
            logger.warning("Notification send failed: %r", exc)


def create_notifier(config: Config) -> Notifier:
    """Build the configured notifier, or a :class:`NullNotifier` when disabled.

    Notifications require ``NOTIFY_ENABLED=true`` and both a bot token and a chat
    id; anything missing yields the no-op notifier (with a warning if enabled but
    incompletely configured), so the app runs unchanged until it's set up.
    """
    if not config.notify_enabled:
        return NullNotifier()
    if not config.notify_bot_token or not config.notify_chat_id:
        logger.warning(
            "NOTIFY_ENABLED is true but NOTIFY_BOT_TOKEN / NOTIFY_CHAT_ID is missing; "
            "notifications are disabled."
        )
        return NullNotifier()
    return TelegramBotNotifier(config.notify_bot_token, config.notify_chat_id)


def format_alert(
    result: PipelineResult, *, channel_name: str, broker: str
) -> str | None:
    """Render a :class:`PipelineResult` as an alert string, or ``None`` to skip.

    Alerts fire for every *recognised* signal or command and its outcome — placed,
    dry-run, not-traded, duplicate, stored (parse-only), management results, and
    failures. Pure noise (:attr:`PipelineStatus.IGNORED`) is skipped so the alerts
    stay meaningful.
    """
    status = result.status
    if status is PipelineStatus.IGNORED:
        return None

    tag = f"[{channel_name}]"

    # A management command (avoid / book profit / move SL) acting on a trade.
    if result.command is not None:
        remarks = result.management.remarks if result.management else ""
        icon = _COMMAND_ICONS.get(status, "🔧")
        return f"{icon} {tag} COMMAND {result.command}\n{status.value}: {remarks}".strip()

    signal = result.signal

    if status is PipelineStatus.DUPLICATE:
        return f"🔁 {tag} Duplicate signal (already seen today)\n{signal}"

    if status is PipelineStatus.MISSED:
        # The point of this alert is the text itself: the user reads it, sees what
        # the parser choked on, and the format gap gets fixed.
        return (
            f"⚠️ {tag} POSSIBLE SIGNAL NOT PARSED — check the format\n"
            f"Nothing was traded. Original message:\n{_quote(result.raw_text)}"
        )

    if status is PipelineStatus.SHADOWED:
        return _format_shadow(result, tag=tag, broker=broker)

    if status is PipelineStatus.STORED:  # parse-only channel
        return f"🗒 {tag} Signal stored (parse-only, not traded)\n{signal}"

    if status is PipelineStatus.NOT_TRADED:
        reason = result.decision.reason if result.decision else "not traded"
        return f"⚪ {tag} Signal NOT traded\n{signal}\nReason: {reason}"

    if status is PipelineStatus.EXECUTED:
        execution = result.execution
        if execution is None:  # defensive; EXECUTED always carries an execution
            return f"🟢 {tag} Order submitted\n{signal}"
        if execution.status is ExecutionStatus.SUCCESS:
            header = "🟢 DRY RUN — would place order" if broker == "dry_run" else "🟢 ORDER PLACED"
        else:  # REJECTED / FAILED
            header = f"❌ Order {execution.status.value.lower()}"
        broker_id = f"\nBroker id: {execution.broker_order_id}" if execution.broker_order_id else ""
        return (
            f"{header} {tag}\n{signal}\n"
            f"Broker: {broker} · {execution.remarks}{broker_id}"
        )

    # Any other (future) status: fall back to a generic line rather than silence.
    return f"ℹ️ {tag} {status.value}\n{signal or ''}".strip()


def _format_shadow(result: PipelineResult, *, tag: str, broker: str) -> str:
    """Render the broker-ready order that shadow mode built and withheld.

    This is the alert the whole ramp-up rests on, so it shows the details that
    decide whether a real order would have been accepted — the resolved contract,
    the expiry, the exchange lot size and the quantity that follows from it, the
    prices, and the funds check — rather than a reassuring summary.
    """
    execution = result.execution
    signal = result.signal
    if execution is None or execution.shadow is None:  # defensive
        return f"👁 {tag} Shadow run\n{signal}"

    report = execution.shadow
    order = execution.order
    if execution.status is not ExecutionStatus.SUCCESS:
        verdict = f"❌ WOULD BE {execution.status.value}"
    elif report.fully_protected:
        verdict = "✅ WOULD GO THROUGH"
    else:
        # The entry is fine but a protective leg is not, which means a position
        # that opens and then cannot be closed on plan. Saying only "would go
        # through" here would be the most misleading thing this alert could do.
        verdict = "⚠️ ENTRY OK — PROTECTION INCOMPLETE"
    lines = [
        f"👁 {tag} SHADOW — no order sent",
        f"{signal}",
        "",
        f"{verdict}",
        f"Symbol: {report.tradingsymbol} ({report.exchange})",
        f"Expiry: {report.expiry.isoformat()}",
        f"Side: {order.transaction_type.value} · {order.order_type.value}",
        f"Qty: {report.quantity} ({report.lots} lot x {report.lot_size})",
        f"Entry: {_price(order.entry_price)} · "
        f"SL: {_price(order.stop_loss)} · Target: {_price(order.target)}",
    ]
    if report.funds_required is not None:
        available = (
            _price(report.funds_available)
            if report.funds_available is not None
            else "unknown"
        )
        lines.append(
            f"Cost: {_price(report.funds_required)} · Available: {available}"
        )
    lines.append(f"Funds: {report.funds_note}")

    if report.protective:
        lines.append("")
        lines.append("Exits (placed after the entry fills):")
        for leg in report.protective:
            mark = "✅" if leg.accepted else "❌"
            price = _price(leg.price)
            lines.append(f"  {mark} {leg.kind}: {leg.order_type} @ {price}")
            if not leg.accepted:
                lines.append(f"     {leg.note}")

    lines.append(f"Broker: {broker}")
    return "\n".join(lines)


def _price(value: float | None) -> str:
    """Render a price for an alert: ``-`` when absent, no trailing ``.0``."""
    if value is None:
        return "-"
    return str(int(value)) if float(value).is_integer() else str(value)


def _quote(text: str | None, *, limit: int = 400) -> str:
    """Quote a raw message for an alert, trimmed to a sensible length."""
    if not text or not text.strip():
        return "(empty)"
    snippet = text.strip()
    if len(snippet) > limit:
        snippet = snippet[:limit] + "…"
    return snippet


#: Icons for management-command outcomes.
_COMMAND_ICONS: dict[PipelineStatus, str] = {
    PipelineStatus.CANCELLED: "🚫",
    PipelineStatus.EXITED: "💰",
    PipelineStatus.MODIFIED: "🔧",
    PipelineStatus.NO_TARGET: "⚠️",
    PipelineStatus.NOT_APPLICABLE: "⚠️",
    PipelineStatus.MGMT_FAILED: "❌",
}


def _http_post(url: str, payload: str) -> None:
    """POST ``payload`` (JSON) to ``url`` (production sender)."""
    request = urllib.request.Request(
        url, data=payload.encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - fixed https API
        response.read()
