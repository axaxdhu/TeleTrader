# Project Progress

Tracks development status of TeleTrader against the phases in `CLAUDE.md`.

## Status

| Phase | Description                  | Status            |
| ----- | ---------------------------- | ----------------- |
| 1     | Telegram listener only       | ✅ Done & verified |
| 2     | Signal parser                | ✅ Done (28 tests) |
| 3     | Database storage             | ⏳ Not started     |
| 4     | Paper trading                | ⏳ Not started     |
| 5     | Live broker integration      | ⏳ Not started     |

> Per `CLAUDE.md`: do not implement later phases unless explicitly requested.

## Phase 1 notes (completed 2026-06-24)

Connects via Telethon, listens to a configured channel, prints + logs each
message. No parsing, broker, or DB.

Verified live: a message sent to Telegram **Saved Messages** appeared in the
terminal and logs.

## Phase 2 notes (completed 2026-06-24)

`src/teletrader/parser.py` — deterministic regex parser, no LLM.

- Fixed signal format (index options only, NIFTY / BANKNIFTY):
  ```
  <UNDERLYING> <STRIKE> <CE|PE> ABOVE <ENTRY>

  SL-<stop_loss>

  TGT-<target>+
  ```
- `Signal` dataclass (frozen, slots): `underlying`, `strike`, `option_type`
  (`OptionType` CE/PE enum), `action` (`Action`, always BUY — ABOVE = breakout
  buy), `entry_price`, `stop_loss`, `target`, `target_open_ended` (the trailing
  `+`), `raw_text`.
- `parse_signal(msg) -> Signal | None`. Returns `None` (logs at DEBUG) for
  noise / malformed / empty / None. Tolerant of double spaces, case, decimals,
  surrounding blank lines. The 3-line structure IS the signal marker.
- Wired into the listener: `_handle_message` parses and prints the `Signal`;
  non-signals print `(ignored)`. **Still no trading.**
- Tests: `tests/test_parser.py` (28 passing) + fixtures in
  `tests/fixtures/sample_messages.md`. Run with `uv run pytest`.
- pytest added as a dev dependency (`[dependency-groups] dev`).

## Git state

- Repo initialised; one commit so far: `Phase 1 - Telegram listener`.
- `.env`, `*.session`, `.venv/` are gitignored and NOT committed.
- **Uncommitted at last hand-off:** `config.py` (numeric-id support, see below)
  and `list_dialogs.py` (untracked helper). Commit these when ready.

## Channel configuration

- `TELEGRAM_CHANNEL` accepts a `@username`, a numeric id, or `me` (Saved Messages).
- Numeric ids must be used **verbatim including the leading `-100…`**
  (Telethon "marked id"), e.g. `-1001163029526`.
- `config.py` has a `_parse_channel()` helper: all-numeric values are coerced to
  `int` (Telethon resolves numeric chats reliably only as ints); `@usernames`
  and `me` stay strings.
- **`list_dialogs.py`** (untracked throwaway) lists all your groups/channels with
  their ids: `uv run python list_dialogs.py`. Copy the id/username into `.env`.
- Current `.env` value: `TELEGRAM_CHANNEL=me` (testing). Real signal channels the
  account is in include e.g. `@safetrader90` / `-1001387252520`, `@BULLCHIP`,
  `-1001163029526` (BULL CHIP PAID). Swap in the desired one for live use.

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

## How to resume in a new chat

1. Point me at this file: "read docs/PROGRESS.md" (it is NOT auto-loaded).
2. `uv sync` if the venv is missing, then `uv run python main.py` to run.
3. Outstanding housekeeping: commit `config.py` + `list_dialogs.py`; optionally
   set a real `TELEGRAM_CHANNEL`; no git remote configured yet.

## Next

Phase 2 = signal parser (regex / deterministic, fixed format, **no AI/LLM**).
