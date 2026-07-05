"""Read-only helper: fetch recent history from a chat and show what parses.

Pulls the last N messages from a Telegram chat and runs each through
``parse_message`` (the same entry point the live listener uses), printing whether
each message is an entry SIGNAL, a management COMMAND, or ignored noise. This is a
diagnostic tool for validating the parser/command regexes against real channel
wording.

Strictly read-only: it does NOT write to the database, evaluate the trade engine,
or place any orders. It reuses the existing .env config and saved Telethon session.

    uv run python fetch_history.py                 # last 50 from TELEGRAM_CHANNEL
    uv run python fetch_history.py --limit 200      # more history
    uv run python fetch_history.py --channel @foo   # a different chat
    uv run python fetch_history.py --signals-only    # hide ignored noise

Safe to delete afterwards.
"""

from __future__ import annotations

import argparse
import asyncio

from telethon import TelegramClient

from teletrader.commands import ManagementCommand
from teletrader.config import Config, _parse_channel
from teletrader.commands import parse_message
from teletrader.parser import Signal


def _classify(text: str) -> tuple[str, str]:
    """Return a (label, detail) pair describing how ``text`` parses."""
    parsed = parse_message(text)
    if isinstance(parsed, Signal):
        return "SIGNAL", str(parsed)
    if isinstance(parsed, ManagementCommand):
        return "COMMAND", str(parsed)
    return "ignored", ""


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=50, help="how many recent messages to fetch")
    ap.add_argument("--channel", default=None, help="chat to read (defaults to TELEGRAM_CHANNEL)")
    ap.add_argument(
        "--signals-only",
        action="store_true",
        help="only print messages that parse as a signal or command",
    )
    ap.add_argument(
        "--raw",
        action="store_true",
        help="dump each message's full raw text, no parsing/classification "
        "(use when inspecting a channel whose format the parser doesn't know yet)",
    )
    args = ap.parse_args()

    cfg = Config.from_env()
    # Numeric marked ids (e.g. -1001524695283) must be int for Telethon to
    # resolve them; a numeric string is treated as a username. cfg.channel is
    # already coerced, so only a --channel override needs it.
    channel = _parse_channel(args.channel) if args.channel is not None else cfg.channel

    client = TelegramClient(cfg.session_name, cfg.api_id, cfg.api_hash)
    await client.start(phone=cfg.phone)

    # get_messages returns newest-first; reverse to read in chronological order.
    messages = await client.get_messages(channel, limit=args.limit)
    messages = list(reversed(messages))

    print(f"Fetched {len(messages)} message(s) from {channel}\n")

    if args.raw:
        # Full, untruncated text of every message — for reverse-engineering an
        # unknown format. Blank-line separated so multi-line messages are clear.
        for msg in messages:
            stamp = msg.date.strftime("%Y-%m-%d %H:%M:%S") if msg.date else "?"
            text = msg.message or ""
            print(f"----- [{stamp}] id={msg.id} -----")
            print(text if text else "(no text / media-only)")
            print()
        await client.disconnect()
        return

    signals = commands = ignored = 0
    for msg in messages:
        text = msg.message or ""
        label, detail = _classify(text)
        if label == "SIGNAL":
            signals += 1
        elif label == "COMMAND":
            commands += 1
        else:
            ignored += 1
            if args.signals_only:
                continue

        stamp = msg.date.strftime("%Y-%m-%d %H:%M:%S") if msg.date else "?"
        if detail:
            print(f"[{stamp}] {label:<7} {detail}")
        else:
            # Ignored noise: show a trimmed preview of the raw text for context.
            preview = " ".join(text.split())[:80]
            print(f"[{stamp}] {label:<7} {preview!r}")

    print(f"\nSummary: {signals} signal(s), {commands} command(s), {ignored} ignored.")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
