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
