"""Telegram listener.

Connects to Telegram via Telethon, subscribes to new messages from a single
configured channel, and hands each message's text to the
:class:`~teletrader.pipeline.SignalPipeline` (parse → store → evaluate →
execute). This module owns only the Telegram I/O; all signal logic lives in the
pipeline and the layers it composes.
"""

from __future__ import annotations

from telethon import TelegramClient, events

from .config import Config
from .logging_config import get_logger
from .pipeline import PipelineResult, PipelineStatus, SignalPipeline

logger = get_logger(__name__)

__all__ = ["TelegramListener"]


class TelegramListener:
    """Listens for new messages on a configured Telegram channel."""

    def __init__(self, config: Config, pipeline: SignalPipeline) -> None:
        self._config = config
        self._pipeline = pipeline
        self._client = TelegramClient(
            config.session_name,
            config.api_id,
            config.api_hash,
        )

    async def run(self) -> None:
        """Start the client and block, handling incoming messages.

        Performs interactive login on first run (prompting for the code sent to
        ``TELEGRAM_PHONE``), then registers the message handler and runs until
        disconnected.
        """
        self._register_handlers()

        logger.info("Connecting to Telegram...")
        await self._client.start(phone=self._config.phone)

        me = await self._client.get_me()
        logger.info("Connected as %s (id=%s)", me.username or me.first_name, me.id)
        logger.info(
            "Listening for new messages on %s (execution mode: %s)",
            self._config.channel,
            self._config.execution_mode,
        )

        await self._client.run_until_disconnected()

    def _register_handlers(self) -> None:
        """Wire up the new-message handler for the configured channel."""

        @self._client.on(events.NewMessage(chats=self._config.channel))
        async def _on_new_message(event: events.NewMessage.Event) -> None:
            self._handle_message(event)

    def _handle_message(self, event: events.NewMessage.Event) -> None:
        """Run a received message through the pipeline and report the outcome.

        Exceptions are caught and logged so a single bad message never tears down
        the listener.
        """
        message = event.message
        text = message.message or ""

        logger.info("Received message id=%s chat_id=%s", message.id, event.chat_id)

        try:
            result = self._pipeline.process(text, when=message.date)
        except Exception:  # noqa: BLE001 - keep the listener alive on any failure
            logger.exception("Failed to process message id=%s", message.id)
            print(f"[{message.date:%H:%M:%S}] (error) {text!r}")
            return

        self._report(result, text, message.date)

    @staticmethod
    def _report(result: PipelineResult, text: str, when) -> None:
        """Print a concise one-line summary for live visibility."""
        stamp = f"[{when:%H:%M:%S}]"
        if result.status is PipelineStatus.IGNORED:
            print(f"{stamp} (ignored) {text!r}")
            return
        if result.status is PipelineStatus.DUPLICATE:
            print(f"{stamp} (duplicate) {result.signal}")
            return

        # Stored: show the signal id + the signal, then the trade outcome.
        print(f"{stamp} SIGNAL #{result.stored_id} {result.signal}")
        if result.status is PipelineStatus.NOT_TRADED:
            assert result.decision is not None
            print(f"           → not traded: {result.decision.reason}")
            return

        assert result.execution is not None  # EXECUTED
        execution = result.execution
        print(f"           → {execution.status.value}: {execution.remarks}")
