# Project Progress

Tracks development status of TeleTrader against the phases in `CLAUDE.md`.

## Status

| Phase | Description                  | Status            |
| ----- | ---------------------------- | ----------------- |
| 1     | Telegram listener only       | ✅ Done & verified |
| 2     | Signal parser                | ⏳ Not started     |
| 3     | Database storage             | ⏳ Not started     |
| 4     | Paper trading                | ⏳ Not started     |
| 5     | Live broker integration      | ⏳ Not started     |

> Per `CLAUDE.md`: do not implement later phases unless explicitly requested.

## Phase 1 notes (completed 2026-06-24)

Connects via Telethon, listens to a configured channel, prints + logs each
message. No parsing, broker, or DB.

Verified live: a message sent to Telegram **Saved Messages** appeared in the
terminal and logs.

## Setup choices

- **uv** for env/deps (not pip/requirements.txt):
  - `uv venv --python 3.12`
  - `uv sync`
  - `uv run python main.py`
- Dependencies live in `pyproject.toml`; `uv.lock` is committed. No `requirements.txt`.
- **src layout**: `src/teletrader/` (`config.py`, `logging_config.py`, `telegram_listener.py`), entry point `main.py`.
- Config via env vars in `.env` (gitignored). Telegram API creds from <https://my.telegram.org>.
- `TELEGRAM_CHANNEL=me` (Saved Messages) is the current value, used for testing — swap to the real signal channel later.
- `teletrader.session` (Telethon login session) exists and is gitignored, so runs no longer prompt for a login code.

## Next

Phase 2 = signal parser (regex / deterministic, fixed format, **no AI/LLM**).
