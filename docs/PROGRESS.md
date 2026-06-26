# Project Progress

Tracks development status of TeleTrader against the phases in `CLAUDE.md`.

## Status

| Phase | Description                  | Status            |
| ----- | ---------------------------- | ----------------- |
| 1     | Telegram listener only       | ✅ Done & verified |
| 2     | Signal parser                | ✅ Done (31 tests) |
| 3     | Database storage             | ✅ Done (17 tests) |
| —     | Trade engine (decision layer)| ✅ Done (15 tests) |
| 4     | Paper trading                | ⏳ Not started     |
| 5     | Live broker integration      | ⏳ Not started     |

> Per `CLAUDE.md`: do not implement later phases unless explicitly requested.
> The trade engine is the broker-agnostic business-logic layer (it *decides*,
> it does not *trade*); it precedes — and feeds — the Phase 4 broker adapter.

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

## Parser change — target offset (2026-06-26)

The stored `target` is **always** set `TARGET_PLUS_OFFSET` (= **2**) points
*below* the level in the message — regardless of any trailing `+` — so the exit
fills before price stalls at the round number (`TGT-200`, `TGT-200+`, `TGT-200++`
all → `target == 198`). The target regex tolerates one or more `+`
(`(?P<open>\++)?`). `target_open_ended` is now a pure **audit** flag recording
whether a `+` was present; it no longer affects the number. `__str__` shows the
adjusted value, e.g. `... TGT 198` / `... TGT 198+`. Suite **49 passing**.

(Earlier iteration applied the offset only when a `+` was present; that was
generalised to always-on at the user's request.)

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

## Trade engine notes (completed 2026-06-26)

Broker-agnostic **business-logic layer**: `Signal → TradeEngine → TradeDecision`.
It decides *whether* a signal should be traded. It does **not** talk to any
broker, place orders, or know about Zerodha/FYERS (kept strictly to the brief).

- `src/teletrader/trade_engine.py`
  - `TradeDecision` (frozen dataclass): `execute: bool`, `quantity: int`,
    `reason: str`. `quantity` is `0` on every rejection.
  - `TradeEngine.evaluate(Signal | None) -> TradeDecision`. Dependencies
    injected: `Config`, `SignalRepository`, optional `rules` tuple, optional
    `clock` (DI for deterministic tests). No globals, no broker.
  - `TradeRule` (runtime-checkable `Protocol`): `name` +
    `check(signal, context) -> str | None` (rejection reason, or `None` to
    allow). Concrete rules, checked in order, first rejection wins:
    `SignalValidityRule` → `AutoTradingRule` → `TradingHoursRule` →
    `DuplicateRule` → `MaxTradesPerDayRule` → `LotSizeRule`. A `None` signal →
    "Malformed".
  - `EvaluationContext` gathers I/O-derived facts once (now, `trades_today`,
    `is_duplicate`, `lot_size`) so each rule stays a pure function — easy to test.
  - **Lot sizing:** options trade in lots, so `quantity = TRADE_LOTS × lot_size`
    (the unit count a broker order wants). Lot size is resolved per underlying
    via a `LotSizeProvider` seam (see below), so NIFTY and BANKNIFTY size
    differently and a missing lot size is a clean rejection ("No lot size
    configured for X"), never a guess.
- **`LotSizeProvider` seam** (`src/teletrader/lot_size.py`): `LotSizeProvider`
  Protocol + `ConfigLotSizeProvider` (serves sizes from `.env`). Injected into
  the engine (defaults to config-backed). **Phase 5 hook:** a
  `BrokerLotSizeProvider` reading the broker instrument master (Kite
  `instruments("NFO")` `lot_size`, or FYERS symbol master) drops in with the
  same interface — no engine change. Documented inline in `lot_size.py`.
- **Config-driven** (all new env vars, safe defaults; `AUTO_TRADING` defaults
  **off**): `AUTO_TRADING`, `ALLOW_DUPLICATES`, `MAX_TRADES_PER_DAY`,
  `TRADE_LOTS`, `LOT_SIZES` (e.g. `NIFTY:65,BANKNIFTY:30`; empty default →
  fail-closed), `MARKET_OPEN_TIME`, `MARKET_CLOSE_TIME`, `MARKET_TIMEZONE`
  (validated IANA tz). New `Config` fields + `_parse_bool/_parse_int/_parse_time/
  _parse_timezone/_parse_lot_sizes` helpers.
- **Repository additions** (additive, backward-compatible): `count_since(moment)`
  (daily-limit rule) and an optional keyword `created_at` on `add()` (so tests —
  and future backfills — can control the stored timestamp).
- **Not wired into the listener.** The engine is a standalone, fully-tested
  decision layer; consuming its `TradeDecision` to actually place/paper-trade is
  Phase 4 (per `CLAUDE.md`, future phases aren't implemented unless requested).
- Tests: `tests/test_trade_engine.py` (15) cover valid signal, malformed
  signal, invalid prices, auto-trading disabled, duplicate (+ allow-duplicates),
  market closed (pre-open and weekend), daily-limit exceeded, yesterday's trades
  not counted, lot sizing (lots × lot size, per-underlying NIFTY vs BANKNIFTY,
  missing-lot-size rejection), and injectable rule sets. Total suite now
  **63 passing** (`uv run pytest`).

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
3. `uv run pytest` to confirm the 63 tests pass.
4. Outstanding housekeeping: optionally set a real `TELEGRAM_CHANNEL` (currently
   `me`); no git remote configured yet.

## Next

Phase 4 = paper trading. Not started; do not implement until explicitly
requested (per `CLAUDE.md`).
