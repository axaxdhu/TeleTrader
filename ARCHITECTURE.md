# Architecture

Technical overview of **TeleTrader** — a Python application that listens to
Telegram trading signals, parses them deterministically, persists them to
SQLite, and runs them through a broker-agnostic **trade engine** that decides
whether each signal should be traded. The system is intentionally broker-agnostic
so that live broker execution (Zerodha Kite first, FYERS later) can be added
without disturbing the ingestion path.

> **Status:** Phases 1–3 implemented (listener → parser → SQLite), plus the
> **trade engine** (the business-logic / decision layer — no broker, no order
> placement). Phases 4–5 (paper trading, live broker) are not yet built. See
> `docs/PROGRESS.md`.

## Component diagram (current)

```
                          ┌──────────────────────────┐
                          │        main.py           │
                          │  (composition root / DI) │
                          └─────────────┬────────────┘
            builds + wires everything   │
        ┌───────────────┬───────────────┼───────────────┬───────────────┐
        ▼               ▼               ▼               ▼               ▼
  ┌───────────┐   ┌───────────┐   ┌───────────┐   ┌───────────┐   ┌───────────┐
  │  config   │   │  logging  │   │ database  │   │repository │   │  parser   │
  │  .py      │   │ _config   │   │  .py      │   │  .py      │   │  .py      │
  └───────────┘   └───────────┘   └─────┬─────┘   └─────┬─────┘   └─────┬─────┘
        │                               │ connect()     │               │
        │ Config                        │ + migrate     │ SignalRepository
        │                               ▼               │               │
        │                         ┌───────────┐         │               │
        │                         │  SQLite   │◄────────┘               │
        │                         │teletrader │  INSERT / SELECT        │
        │                         │   .db     │                         │
        │                         └───────────┘                         │
        ▼                                                               ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │                        telegram_listener.py                           │
  │                          TelegramListener                             │
  │                                                                       │
  │   Telegram (Telethon)  ──►  parse_signal()  ──►  repository.add()     │
  │        new message          (parser.py)         (repository.py)       │
  └──────────────────────────────────────────────────────────────────────┘
                                     │
                                     ▼  Signal
                          ┌───────────────────────┐
                          │   trade_engine.py     │   business logic only —
                          │     TradeEngine       │   NO broker, NO orders.
                          │  composes TradeRules  │   reads repo for dedupe
                          │   → TradeDecision     │   + daily-count checks
                          └───────────────────────┘
```

### Runtime data flow

```
Telegram message
   → TelegramListener._handle_message   (telegram_listener.py)
       → parse_signal(text)             (parser.py)        → Signal | None
           → SignalRepository.add(sig)  (repository.py)    → StoredSignal
               → INSERT INTO signals    (SQLite)
                   ↑ UNIQUE(message_hash) rejects duplicates
   → console output: SIGNAL #id / (duplicate) / (ignored)

Decision layer (broker-agnostic, no order placement):
   TradeEngine.evaluate(Signal)         (trade_engine.py)  → TradeDecision
       → SignalValidityRule → AutoTradingRule → TradingHoursRule
         → DuplicateRule → MaxTradesPerDayRule   (first rejection wins)
   TradeDecision(execute, quantity, reason)
       (the future broker adapter acts on this; Phases 1–3 ingestion is unchanged)
```

A message that is not a valid signal is dropped at the parser (`None`); a
signal whose content hash already exists is rejected at the repository
(`DuplicateSignalError`). Neither case raises out of the listener. The
`TradeEngine` is a pure decision-maker: given a `Signal` it returns a
`TradeDecision` and never performs I/O beyond consulting the repository for
duplicate/daily-count facts.

## Modules & responsibilities

| Module | Responsibility |
| --- | --- |
| `main.py` | **Composition root.** Loads config, configures logging, opens + initializes the SQLite connection, constructs the repository, injects it into the listener, and runs the asyncio loop. Owns no business logic. |
| `src/teletrader/config.py` | Loads immutable `Config` from environment variables (`.env` via `python-dotenv`). Validates required values, coerces types (numeric channel ids, `api_id`), raises `ConfigError` on missing/invalid config. **Secrets never live in code.** |
| `src/teletrader/logging_config.py` | Centralized structured logging. `configure_logging(level)` (idempotent) and `get_logger(name)`. Quiets Telethon below DEBUG. |
| `src/teletrader/telegram_listener.py` | `TelegramListener` — connects via Telethon, subscribes to one configured chat, and orchestrates **parse → persist** per message. Handles login/session. Prints results for live visibility. No parsing or SQL of its own. |
| `src/teletrader/parser.py` | Pure, deterministic signal parsing (regex only, **no AI/LLM**). `parse_signal(str) -> Signal \| None`. Defines the `Signal` dataclass and `Action`/`OptionType` enums. Applies the always-on target offset (`TARGET_PLUS_OFFSET`). No I/O. |
| `src/teletrader/database.py` | Owns *where* data lives: `connect(path)` (tuned SQLite connection) and `initialize(conn)` (forward-only migrations keyed off `PRAGMA user_version`). Holds the SQL schema. No knowledge of `Signal`. |
| `src/teletrader/repository.py` | Owns *what* is stored: `SignalRepository` (CRUD over the `signals` table), `StoredSignal`, `signal_hash()` (dedupe key), `DuplicateSignalError`. Maps `Signal` ↔ rows. The only module that issues SQL against `signals`. Also exposes `count_since()` for the engine's daily-limit rule. |
| `src/teletrader/trade_engine.py` | **Business-logic / decision layer.** `TradeEngine.evaluate(Signal) -> TradeDecision`. Composes config-driven `TradeRule`s (validity, auto-trading toggle, trading hours, duplicate, daily limit). **Broker-agnostic: no Zerodha/FYERS, no order placement.** Knows nothing of how a decision is executed. |
| `list_dialogs.py` | Standalone dev helper to list Telegram chats/ids for `.env` configuration. Not part of the runtime path. |
| `tests/` | `test_parser.py`, `test_repository.py`, `test_trade_engine.py` — unit tests run with `uv run pytest`. |

## Interfaces between modules

Boundaries are small and explicit, so a module can be replaced without touching
its neighbours.

- **config → everything**: `Config` (frozen dataclass) is read-only data. The
  rest of the system depends on this shape, not on environment variables
  directly.
  ```python
  Config(api_id, api_hash, phone, session_name, channel, log_level, database_path,
         auto_trading, allow_duplicates, max_trades_per_day, trade_quantity,
         market_open, market_close, market_timezone)
  ```
  The last seven fields drive the trade engine; all come from env vars with safe
  defaults (`AUTO_TRADING` defaults **off**).

- **parser → listener / repository**: the `Signal` dataclass is the lingua
  franca of the system.
  ```python
  parse_signal(message: str | None) -> Signal | None
  ```
  `Signal` is frozen + slotted: `underlying, strike, option_type, action,
  entry_price, stop_loss, target, target_open_ended, raw_text`.

- **database → repository / main**: connection lifecycle is separate from
  persistence logic.
  ```python
  connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection
  initialize(connection: sqlite3.Connection) -> int   # returns schema version
  ```

- **repository → listener**: the persistence contract. The repository is
  constructed with an injected connection (DI) and exposes:
  ```python
  SignalRepository(connection: sqlite3.Connection)
  .add(signal: Signal) -> StoredSignal        # raises DuplicateSignalError
  .exists(signal: Signal) -> bool
  .get(signal_id: int) -> StoredSignal | None
  .list_all() -> list[StoredSignal]
  .count() -> int
  signal_hash(signal: Signal) -> str          # deterministic SHA-256
  ```

- **listener → Telegram**: Telethon's `TelegramClient` + `events.NewMessage`.
  This is the one place the external Telegram API is touched; everything
  downstream sees only plain `str` message text.

- **trade engine → (future) broker adapter**: the decision contract. The engine
  is constructed with injected `Config`, `SignalRepository`, an optional rule
  set, and an optional clock (DI), and exposes a single pure decision method:
  ```python
  TradeEngine(config, repository, *, rules=None, clock=None)
  .evaluate(signal: Signal | None) -> TradeDecision
  TradeDecision(execute: bool, quantity: int, reason: str)
  ```
  A `TradeRule` is anything with a `name` and
  `check(signal, context) -> str | None` (a rejection reason, or `None` to
  allow). The rule set is the extension point for future risk rules; the engine
  itself contains no broker knowledge.

### Persistence schema — `signals`

| column | type | notes |
| --- | --- | --- |
| `id` | INTEGER | PK, autoincrement |
| `message_hash` | TEXT | **UNIQUE** — deterministic dedupe key (SHA-256 hex) |
| `underlying` | TEXT | NIFTY / BANKNIFTY |
| `strike` | INTEGER | |
| `option_type` | TEXT | CE / PE |
| `action` | TEXT | BUY |
| `entry_price` | REAL | |
| `stop_loss` | REAL | |
| `target` | REAL | stored value (always offset below the raw level) |
| `target_open_ended` | INTEGER | 0/1 — audit flag: did the message carry a `+` |
| `raw_text` | TEXT | original message, for audit |
| `created_at` | TEXT | ISO-8601 UTC, row insertion time |

## Key design decisions

1. **Layered, broker-agnostic pipeline** — *Telegram → Parser → Risk → Broker →
   DB → Notifications* is the target. Each stage has one responsibility and
   talks to the next only through small data types (`Signal`, `StoredSignal`).
   This lets the future Risk Manager and Broker Adapter slot in without
   rewriting ingestion, and lets Zerodha/FYERS sit behind one adapter interface.

2. **Deterministic regex parsing, no LLM** — signals have a fixed format, so
   parsing is pure regex in `parser.py`. Reasons: predictability,
   testability (51 unit tests), zero per-message cost/latency, and no external
   dependency or non-determinism in a path that will eventually place real
   trades. Non-matching messages are rejected gracefully (`None`), never raise.

3. **`database.py` (where) vs `repository.py` (what)** — connection/schema
   concerns are deliberately separated from row mapping and dedupe logic. The
   repository never opens a connection; `database` never imports `Signal`. This
   keeps SQL in one place and makes both independently testable.

4. **Dependency injection of the connection** — the repository receives its
   `sqlite3.Connection` rather than creating one. Tests pass `":memory:"` for
   fast, isolated runs; production shares a single WAL connection. `main.py` is
   the only wiring point.

5. **Deterministic content hash for deduplication** — `signal_hash()` hashes the
   *structured* fields (not `raw_text`) in a canonical order, and the column is
   `UNIQUE`. So the same trade reposted with different whitespace/casing/chatter
   is recognised as a duplicate, while genuinely different signals are not.
   Enforcing it as a DB constraint (not just an app check) makes the database
   the source of truth and is race-safe.

6. **Forward-only migrations via `PRAGMA user_version`** — no ORM, no external
   migration tool (keeps it simple, per project principles). `initialize()` is
   idempotent and safe to call on every startup; evolving the schema is just
   appending one SQL statement to `_MIGRATIONS`.

7. **SQLite over an external database** — single-file, zero-ops, sufficient for
   a single-user signal log. WAL mode is enabled for safer concurrent reads
   while the listener writes. The file is gitignored.

8. **Config exclusively from environment variables** — all secrets (API id/hash,
   phone) and tunables (channel, db path, log level) come from `.env`/env, never
   from code. `Config` is a frozen dataclass so config can't mutate at runtime.

9. **Always-on target offset** — an open-ended target (`TGT-200`, `TGT-200+`,
   `TGT-200++`) is stored `TARGET_PLUS_OFFSET` (2) points below the quoted
   level so the eventual exit fills before price stalls at a round number. The
   `+` is preserved only as the `target_open_ended` audit flag and no longer
   affects the number.

10. **Type hints + frozen dataclasses everywhere** — `Signal`, `StoredSignal`,
    `Config`, `TradeDecision` are all `frozen=True, slots=True`. Immutable value
    objects flowing between layers prevent accidental mutation and make the data
    flow easy to reason about.

11. **Trade engine = pure decision layer, rules as composable units** — the
    engine answers *should we trade this?* and nothing else: it returns a
    `TradeDecision` and never places an order or imports a broker SDK. Each
    policy (validity, auto-trading toggle, trading hours, duplicate, daily
    limit) is an independent `TradeRule` checked in order; the first rejection
    wins. Rules are config-driven and the rule set is injectable, so future risk
    rules slot in without modifying the engine. I/O-derived facts (now,
    today's count, duplicate?) are gathered once into an immutable
    `EvaluationContext`, keeping each rule a pure, trivially testable function.

## Where future phases plug in

- **Phase 4 (paper trading)** / **Phase 5 (live broker)**: a `BrokerAdapter`
  interface sits between the engine and the broker. The flow becomes
  *parse → `repository.add()` → `TradeEngine.evaluate()` → (if `execute`)
  `BrokerAdapter.place(decision)`*. The engine is already built and decoupled;
  only the adapter and the listener wiring remain. Zerodha/FYERS sit behind the
  one adapter interface.
- **More risk rules**: append `TradeRule`s (position sizing, per-underlying
  caps, exposure limits) to the engine's rule set — no engine changes needed.
- **Notifications**: a thin sink subscribed to `TradeDecision` / stored-signal
  events.
