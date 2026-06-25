"""One-off helper: list your Telegram groups/channels with their IDs.

Reuses the existing .env config and saved session. Run with:
    uv run python list_dialogs.py
Then copy the id (or @username) you want into TELEGRAM_CHANNEL in .env.
Safe to delete afterwards.
"""

from __future__ import annotations

import asyncio

from telethon import TelegramClient

from teletrader.config import Config


async def main() -> None:
    cfg = Config.from_env()
    client = TelegramClient(cfg.session_name, cfg.api_id, cfg.api_hash)
    await client.start(phone=cfg.phone)

    print(f"{'ID':>16}  {'TYPE':<10} {'USERNAME':<24} TITLE")
    print("-" * 80)
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if dialog.is_group:
            kind = "group"
        elif dialog.is_channel:
            kind = "channel"
        else:
            continue  # skip private chats / bots
        username = getattr(entity, "username", None)
        username = f"@{username}" if username else "-"
        print(f"{dialog.id:>16}  {kind:<10} {username:<24} {dialog.name}")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
