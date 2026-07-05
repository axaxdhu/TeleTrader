"""Telegram listener.

Connects to Telegram via Telethon and subscribes to one or more configured
channels, handing each message's text to that channel's
:class:`~teletrader.pipeline.MessageProcessor` (the full trading pipeline for the
primary channel, a parse-only pipeline for the second). This module owns only the
Telegram I/O; all signal logic lives in the pipelines and the layers they compose.

Each channel gets its own Telethon handler scoped to that channel, so routing is
handled by Telethon's own filtering — there is no manual ``chat_id`` dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass

from telethon import TelegramClient, events

from .config import Config
from .logging_config import get_logger
from .pipeline import MessageProcessor, PipelineResult, PipelineStatus

logger = get_logger(__name__)

__all__ = ["ChannelSubscription", "TelegramListener"]


@dataclass(frozen=True, slots=True)
class ChannelSubscription:
    """One channel the listener watches, and how to process its messages.

    ``name`` is a short label for logs (e.g. ``"channel1"``); ``channel`` is the
    Telethon chat identifier (numeric id, ``@username``, or ``me``); ``processor``
    turns a message into a :class:`PipelineResult`.
    """

    name: str
    channel: int | str
    processor: MessageProcessor


class TelegramListener:
    """Listens for new messages on one or more configured Telegram channels."""

    def __init__(
        self, config: Config, subscriptions: list[ChannelSubscription]
    ) -> None:
        if not subscriptions:
            raise ValueError("TelegramListener needs at least one channel subscription")
        self._config = config
        self._subscriptions = subscriptions
        self._client = TelegramClient(
            config.session_name,
            config.api_id,
            config.api_hash,
            # Keep a long-running listener alive across idle/stale connections
            # (e.g. a pre-market socket dropped by the server at open):
            # reconnect forever rather than giving up after the default 5 tries
            # and exiting. retry_delay bounds attempts to one every 5s.
            connection_retries=-1,
            retry_delay=5,
            auto_reconnect=True,
        )

    async def run(self) -> None:
        """Start the client and block, handling incoming messages.

        Performs interactive login on first run (prompting for the code sent to
        ``TELEGRAM_PHONE``), then registers a handler per subscribed channel and
        runs until disconnected.
        """
        self._register_handlers()

        logger.info("Connecting to Telegram...")
        await self._client.start(phone=self._config.phone)

        me = await self._client.get_me()
        logger.info("Connected as %s (id=%s)", me.username or me.first_name, me.id)
        for sub in self._subscriptions:
            logger.info("Listening on %s (%s)", sub.channel, sub.name)
        logger.info("Execution mode: %s", self._config.execution_mode)

        await self._client.run_until_disconnected()

        # We only reach here once the client is fully disconnected and its
        # auto-reconnect has given up (or been stopped). With connection_retries
        # set to infinite this should effectively never happen short of a
        # deliberate stop -- so if it does appear in the log, the listener has
        # exited and is no longer receiving messages.
        logger.warning(
            "Disconnected from Telegram; listener is stopping. "
            "No further messages will be received until restarted."
        )

    def _register_handlers(self) -> None:
        """Wire up a new-message handler per subscribed channel.

        Each handler is scoped to its own channel so Telethon routes messages;
        the subscription is bound via a default argument to avoid the classic
        late-binding closure trap in the loop.
        """
        for sub in self._subscriptions:

            @self._client.on(events.NewMessage(chats=sub.channel))
            async def _on_new_message(
                event: events.NewMessage.Event, _sub: ChannelSubscription = sub
            ) -> None:
                self._handle_message(event, _sub)

    def _handle_message(
        self, event: events.NewMessage.Event, sub: ChannelSubscription
    ) -> None:
        """Run a received message through its channel's processor and report.

        Exceptions are caught and logged so a single bad message never tears down
        the listener.
        """
        message = event.message
        text = message.message or ""

        logger.info(
            "Received message id=%s chat_id=%s (%s)",
            message.id,
            event.chat_id,
            sub.name,
        )

        try:
            result = sub.processor.process(text, when=message.date)
        except Exception:  # noqa: BLE001 - keep the listener alive on any failure
            logger.exception("Failed to process message id=%s", message.id)
            print(f"[{message.date:%H:%M:%S}] ({sub.name}) (error) {text!r}")
            return

        self._report(result, text, message.date, sub.name)

    @staticmethod
    def _report(
        result: PipelineResult, text: str, when, channel_name: str
    ) -> None:
        """Print a concise one-line summary for live visibility."""
        stamp = f"[{when:%H:%M:%S}] ({channel_name})"
        if result.status is PipelineStatus.IGNORED:
            print(f"{stamp} (ignored) {text!r}")
            return

        # A management command (avoid / book profit / move SL) acting on a trade.
        if result.command is not None:
            remarks = result.management.remarks if result.management else ""
            print(f"{stamp} COMMAND {result.command}")
            print(f"           → {result.status.value}: {remarks}")
            return

        if result.status is PipelineStatus.DUPLICATE:
            print(f"{stamp} (duplicate) {result.signal}")
            return

        # Stored: show the signal id + the signal, then the outcome.
        print(f"{stamp} SIGNAL #{result.stored_id} {result.signal}")
        if result.status is PipelineStatus.STORED:
            print("           → stored (parse-only, not traded)")
            return
        if result.status is PipelineStatus.NOT_TRADED:
            assert result.decision is not None
            print(f"           → not traded: {result.decision.reason}")
            return

        assert result.execution is not None  # EXECUTED
        execution = result.execution
        print(f"           → {execution.status.value}: {execution.remarks}")
