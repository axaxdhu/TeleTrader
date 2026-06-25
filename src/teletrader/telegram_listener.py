"""Telegram listener.

Connects to Telegram via Telethon, subscribes to new messages from a single
configured channel, and prints + logs each message. Deliberately does no
parsing, trading, or persistence (Phase 1 scope).
"""

from __future__ import annotations

from telethon import TelegramClient, events

from .config import Config
from .logging_config import get_logger
from .parser import parse_signal

logger = get_logger(__name__)


class TelegramListener:
    """Listens for new messages on a configured Telegram channel."""

    def __init__(self, config: Config) -> None:
        self._config = config
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
        logger.info("Listening for new messages on %s", self._config.channel)

        await self._client.run_until_disconnected()

    def _register_handlers(self) -> None:
        """Wire up the new-message handler for the configured channel."""

        @self._client.on(events.NewMessage(chats=self._config.channel))
        async def _on_new_message(event: events.NewMessage.Event) -> None:
            self._handle_message(event)

    def _handle_message(self, event: events.NewMessage.Event) -> None:
        """Parse a received message into a structured signal and report it.

        Non-signal messages (noise) are ignored. No trading is performed — this
        only prints/logs the parsed result.
        """
        message = event.message
        text = message.message or ""

        logger.info(
            "Received message id=%s chat_id=%s",
            message.id,
            event.chat_id,
        )

        signal = parse_signal(text)
        if signal is None:
            # Noise / malformed — already logged by the parser.
            print(f"[{message.date:%H:%M:%S}] (ignored) {text!r}")
            return

        # Console output for immediate visibility during development.
        print(f"[{message.date:%H:%M:%S}] SIGNAL {signal}")
