# Project Progress

Tracks development status of TeleTrader against the phases in `CLAUDE.md`.

## Status

| Phase | Description                  | Status            |
| ----- | ---------------------------- | ----------------- |
| 1     | Telegram listener only       | ✅ Done & verified |
| 2     | Signal parser                | ✅ Done (31 tests) |
| 3     | Database storage             | ✅ Done (17 tests) |
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
- `Signal.__str__` renders a compact one-liner used for console + log output,
  e.g. `BUY NIFTY 23900 PE @165 SL 150 TGT 200+` (trailing `.0` dropped on whole
  numbers, decimals kept). Full data still available via `__repr__`/fields.
- Wired into the listener: `_handle_message` parses and prints the `Signal`;
  non-signals print `(ignored)`. **Still no trading.**
- Tests: `tests/test_parser.py` (31 passing) + fixtures in
  `tests/fixtures/sample_messages.md`. Run with `uv run pytest`.
- pytest added as a dev dependency (`[dependency-groups] dev`).

## Parser change — open-ended target offset (2026-06-26)

A trailing `+` on the target (e.g. `TGT-200+`) no longer stores the raw level.
The stored `target` is now set `TARGET_PLUS_OFFSET` (= **2**) points *below* the
signal value (`TGT-200+` → `target == 198`) so the exit fills before price
stalls at the round number. The `target_open_ended` flag is **retained** (set
`True`) purely for audit, so you can still see the original carried a `+`.
Explicit targets (no `+`) are taken verbatim. `__str__` reflects the adjusted
value, e.g. `... TGT 198+`. Covered by added tests; suite now **50 passing**.

## Phase 3 notes (completed 2026-06-26)

SQLite persistence layer: **Telegram message → parser → SQLite**. No trading,
no broker integration (kept strictly to phase scope).

- `src/teletrader/database.py` — owns the connection + schema.
  - `connect(path)` opens a `sqlite3` connection (`Row` factory, `foreign_keys`
    + WAL pragmas). Pass `":memory:"` for tests.
  - `initialize(conn)` runs forward-only migrations keyed off the built-in
    `PRAGMA user_version`. Idempotent — safe to call on every startup. To evolve
    the schema, append SQL to `_MIGRATIONS`; `SCHEMA_VERSION` derives from its
    length.
  - `DEFAULT_DB_PATH = "teletrader.db"` (gitignored, alongside `*.db-wal/-shm`).
- `src/teletrader/repository.py` — `SignalRepository` (connection injected, DI).
  - `add(signal) -> StoredSignal` inserts and returns the row (id + created_at);
    raises `DuplicateSignalError` on a repeat. Also `exists`, `get`, `list_all`,
    `count`.
  - `StoredSignal` (frozen dataclass): `id`, `message_hash`, `created_at`
    (tz-aware UTC), and the original `Signal`.
  - **Deterministic dedupe:** `signal_hash(signal)` is a SHA-256 over the
    *structured* fields in canonical order (not `raw_text`), so the same trade
    re-posted with different whitespace/casing/surrounding chatter collides;
    `165` and `165.0` hash identically. Enforced by a `UNIQUE(message_hash)`
    column — DB is the source of truth, not just an app-level check.
- Wiring: `Config` gained `database_path` (`DATABASE_PATH` env, default
  `teletrader.db`); `main.py` builds one connection + repo and injects it into
  `TelegramListener(config, repository)`. The listener now parses → `add()`,
  printing `SIGNAL #<id>`, `(duplicate)`, or `(ignored)`.
- Tests: `tests/test_repository.py` (17) cover schema/migration idempotency,
  CRUD, duplicate rejection, whitespace-insensitive dedupe, and hash
  determinism. Total suite now **48 passing** (`uv run pytest`).

### Schema — `signals`

| column              | type    | notes                                  |
| ------------------- | ------- | -------------------------------------- |
| `id`                | INTEGER | PK, autoincrement                      |
| `message_hash`      | TEXT    | **UNIQUE** — dedupe key (sha256 hex)   |
| `underlying`        | TEXT    | NIFTY / BANKNIFTY                      |
| `strike`            | INTEGER |                                        |
| `option_type`       | TEXT    | CE / PE                                |
| `action`            | TEXT    | BUY                                    |
| `entry_price`       | REAL    |                                        |
| `stop_loss`         | REAL    |                                        |
| `target`            | REAL    |                                        |
| `target_open_ended` | INTEGER | 0/1 (the trailing `+`)                 |
| `raw_text`          | TEXT    | original message, for audit            |
| `created_at`        | TEXT    | ISO-8601 UTC                           |

### Project structure (updated)

```
main.py                         # config → connect/initialize → repo → listener
src/teletrader/
  config.py                     # + database_path (DATABASE_PATH env)
  database.py                   # NEW — connect() + migrations (user_version)
  repository.py                 # NEW — SignalRepository, StoredSignal, signal_hash
  parser.py                     # Phase 2 (unchanged)
  telegram_listener.py          # now: parse → repo.add(), dedupe-aware
  logging_config.py
tests/
  test_parser.py                # 31
  test_repository.py            # NEW — 17
```

## Git state

- `.env`, `*.session`, `.venv/` are gitignored and NOT committed.
- No git remote configured yet. Commits so far (newest first):
  - `00e6b0d` Add compact Signal string formatting
  - `bb62074` Phase 2 - Signal parser
  - `be2856e` Support numeric channel IDs and add dialog-listing helper
  - `b54c827` Phase 1 - Telegram listener
- Working tree clean.

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
3. `uv run pytest` to confirm the 31 tests pass.
4. Outstanding housekeeping: optionally set a real `TELEGRAM_CHANNEL` (currently
   `me`); no git remote configured yet.

## Next

Phase 4 = paper trading. Not started; do not implement until explicitly
requested (per `CLAUDE.md`).
