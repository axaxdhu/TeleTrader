# Architecture

Technical overview of **TeleTrader** — a Python application that listens to
Telegram trading signals, parses them deterministically, persists them to
SQLite, runs them through a broker-agnostic **trade engine** that decides whether
each signal should be traded, and hands accepted orders to an **execution layer**
that submits them. The trading *strategy* lives outside this system (in the
Telegram signals); TeleTrader's job is to **execute those signals reliably**. The
execution layer is broker-independent so live execution (Zerodha Kite first,
FYERS later) can be added without disturbing the ingestion or decision path.

> **Status:** Phases 1–3 implemented (listener → parser → SQLite), plus the
> **trade engine** (decision layer — no order placement) and the **execution
> layer**: an `Executor` interface with `DryRunExecutor` (validates + logs +
> records execution attempts to SQLite; sends nothing to any broker) as the
> first implementation. `EXECUTION_MODE` selects the executor; the live
> `KiteExecutor` (`kite`) arrives next phase. The whole path is now **wired
> end-to-end** through a `SignalPipeline` — a live message runs *parse → store →
> evaluate → execute (dry run)*. See `docs/PROGRESS.md`.
>
> *(An earlier iteration built a simulated `PaperBroker` that modelled fills,
> positions, and P&L; that was removed in favour of this leaner execution layer —
> the system executes externally-defined signals, it does not simulate a market.)*

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
   → TelegramListener._handle_message   (telegram_listener.py)  — Telegram I/O only
       → SignalPipeline.process(text)   (pipeline.py)           — orchestration
           → parse_signal(text)         (parser.py)        → Signal | None  ── None → IGNORED
           → TradeEngine.evaluate(sig)  (trade_engine.py)  → TradeDecision   (sees prior history)
           → SignalRepository.add(sig)  (repository.py)    → StoredSignal
               → INSERT INTO signals    (SQLite)
                   ↑ UNIQUE(message_hash, trade_date) → DuplicateSignalError → DUPLICATE
           → if not decision.execute:                       → NOT_TRADED (signal still stored)
           → build_order_request(sig, decision)             → OrderRequest
           → Executor.execute(order)    (execution/)        → ExecutionResult → EXECUTED
   → console output: SIGNAL #id + outcome / (duplicate) / (ignored)
```

The engine evaluates **before** the signal is stored, so its duplicate/daily-count
checks see only prior history (not the row being processed); storage is the
authoritative same-day dedupe.

```
Decision layer (broker-agnostic, no order placement):
   TradeEngine.evaluate(Signal)         (trade_engine.py)  → TradeDecision
       → SignalValidityRule → AutoTradingRule → TradingHoursRule
         → DuplicateRule → MaxTradesPerDayRule → LotSizeRule  (first rejection wins)
   TradeDecision(execute, quantity = lots × lot_size, reason)

Execution layer (broker-independent; submits orders, no market simulation):
   Executor.execute(OrderRequest)       (execution/)       → ExecutionResult
       → validate_order() → log "[DRY RUN] …" → ExecutionRepository.add()
   DryRunExecutor records the attempt (SUCCESS / REJECTED) and sends nothing.
   (KiteExecutor will submit to the broker behind the same interface — next phase.)
```

A message that is not a valid signal is dropped at the parser (`None`); a
signal whose content was already stored **on the same trading day** is rejected
at the repository (`DuplicateSignalError`) — the same signal on a later day is
accepted. Neither case raises out of the listener. The
`TradeEngine` is a pure decision-maker: given a `Signal` it returns a
`TradeDecision` and never performs I/O beyond consulting the repository for
duplicate/daily-count facts.

## Modules & responsibilities

| Module | Responsibility |
| --- | --- |
| `main.py` | **Composition root.** Loads config, configures logging, opens + initializes the SQLite connection, constructs the repository, injects it into the listener, and runs the asyncio loop. Owns no business logic. |
| `src/teletrader/config.py` | Loads immutable `Config` from environment variables (`.env` via `python-dotenv`). Validates required values, coerces types (numeric channel ids, `api_id`), raises `ConfigError` on missing/invalid config. **Secrets never live in code.** |
| `src/teletrader/logging_config.py` | Centralized structured logging. `configure_logging(level)` (idempotent) and `get_logger(name)`. Quiets Telethon below DEBUG. |
| `src/teletrader/telegram_listener.py` | `TelegramListener` — connects via Telethon, subscribes to one configured chat, and hands each message's text to the `SignalPipeline`. Handles login/session; prints a one-line outcome per message. Owns only Telegram I/O — no parsing, SQL, or trading logic. |
| `src/teletrader/pipeline.py` | `SignalPipeline` — orchestrates one message: **parse → store → evaluate → execute**. Telethon-free (so it is unit-tested directly). Holds `build_order_request()`, the seam mapping a `Signal` + `TradeDecision` to an `OrderRequest`, so the decision and execution layers don't depend on each other's types. Returns a `PipelineResult` the listener presents. |
| `src/teletrader/parser.py` | Pure, deterministic signal parsing (regex only, **no AI/LLM**). `parse_signal(str) -> Signal \| None`. Defines the `Signal` dataclass and `Action`/`OptionType` enums. Applies the always-on target offset (`TARGET_PLUS_OFFSET`). No I/O. |
| `src/teletrader/database.py` | Owns *where* data lives: `connect(path)` (tuned SQLite connection) and `initialize(conn)` (forward-only migrations keyed off `PRAGMA user_version`). Holds the SQL schema. No knowledge of `Signal`. |
| `src/teletrader/repository.py` | Owns *what* is stored: `SignalRepository` (CRUD over the `signals` table), `StoredSignal`, `signal_hash()` (dedupe key), `DuplicateSignalError`. Maps `Signal` ↔ rows. The only module that issues SQL against `signals`. Also exposes `count_since()` for the engine's daily-limit rule. |
| `src/teletrader/trade_engine.py` | **Business-logic / decision layer.** `TradeEngine.evaluate(Signal) -> TradeDecision`. Composes config-driven `TradeRule`s (validity, auto-trading toggle, trading hours, duplicate, daily limit, lot size). Sizes the order as `lots × lot_size`. **Broker-agnostic: no Zerodha/FYERS, no order placement.** Knows nothing of how a decision is executed. |
| `src/teletrader/lot_size.py` | The `LotSizeProvider` seam: resolves an underlying's exchange lot size. `ConfigLotSizeProvider` serves it from `.env` today; a `BrokerLotSizeProvider` (Phase 5) will read it from the broker instrument master — same interface, no engine change. |
| `src/teletrader/execution/` | **Order execution layer.** The broker-independent seam between the decision layer and a broker. `base.py` — abstract `Executor` (`mode`, `execute(OrderRequest) -> ExecutionResult`). `models.py` — `OrderRequest`, `ExecutionResult`, and enums (`ExecutionStatus`, `OrderType`, `ProductType`, `TransactionType`). `validation.py` — `validate_order()` (shared gate, raises `InvalidOrderError`). `dry_run.py` — `DryRunExecutor` (see below). `repository.py` — `ExecutionRepository` over the `executions` table. `exceptions.py` — the `ExecutionError` hierarchy. `factory.py` — `create_executor(config, repo)` picks the executor from `EXECUTION_MODE`. Independent of Telegram and of any broker SDK. |
| `src/teletrader/execution/dry_run.py` | **`DryRunExecutor` — first concrete `Executor` (Phase 4).** Validates an `OrderRequest`, logs **exactly what would be submitted** (`[DRY RUN] …`), and records the attempt to SQLite — but contacts **no broker** and models no fills/positions/P&L. A valid order → `SUCCESS` ("would have been submitted"); an invalid one → `REJECTED` with the reason. Exists to validate the pipeline end-to-end before live trading; the future `KiteExecutor` replaces it behind the same interface by changing `EXECUTION_MODE`. |
| `list_dialogs.py` | Standalone dev helper to list Telegram chats/ids for `.env` configuration. Not part of the runtime path. |
| `tests/` | `test_parser.py`, `test_repository.py`, `test_trade_engine.py`, `test_executor.py`, `test_execution_repository.py` — unit tests run with `uv run pytest`. |

## Interfaces between modules

Boundaries are small and explicit, so a module can be replaced without touching
its neighbours.

- **config → everything**: `Config` (frozen dataclass) is read-only data. The
  rest of the system depends on this shape, not on environment variables
  directly.
  ```python
  Config(api_id, api_hash, phone, session_name, channel, log_level, database_path,
         auto_trading, allow_duplicates, max_trades_per_day, trade_lots,
         lot_sizes, market_open, market_close, market_timezone,
         execution_mode)
  ```
  The eight `auto_trading … market_timezone` fields drive the trade engine;
  `execution_mode` (`dry_run` | `kite`) selects the executor. All come from env
  vars with safe defaults (`AUTO_TRADING` defaults **off**; `lot_sizes` defaults
  **empty**, so an underlying with no configured lot size is rejected rather than
  mis-sized; `EXECUTION_MODE` defaults **`dry_run`**).

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
  SignalRepository(connection: sqlite3.Connection, *, tz: tzinfo = timezone.utc)
  .add(signal: Signal) -> StoredSignal              # raises DuplicateSignalError (same day)
  .exists(signal: Signal, *, on_date: date | None = None) -> bool
  .get(signal_id: int) -> StoredSignal | None
  .list_all() -> list[StoredSignal]
  .count() -> int
  signal_hash(signal: Signal) -> str                # deterministic SHA-256 (content only)
  ```
  Dedupe is **per trading day**: `add` rejects only a same-day repeat; `exists`
  checks a given date (defaulting to today in `tz`). Inject the market timezone
  (`main.py` does) so "day" is the trading day.

- **listener → Telegram**: Telethon's `TelegramClient` + `events.NewMessage`.
  This is the one place the external Telegram API is touched; everything
  downstream sees only plain `str` message text.

- **trade engine → executor**: the decision contract. The engine is constructed
  with injected `Config`, `SignalRepository`, and optional `LotSizeProvider`, rule
  set, and clock (DI), and exposes a single pure decision method:
  ```python
  TradeEngine(config, repository, *, lot_size_provider=None, rules=None, clock=None)
  .evaluate(signal: Signal | None) -> TradeDecision
  TradeDecision(execute: bool, quantity: int, reason: str)   # quantity = lots × lot_size
  ```
  A `TradeRule` is anything with a `name` and
  `check(signal, context) -> str | None` (a rejection reason, or `None` to
  allow). The engine itself contains no broker knowledge.

- **executor (execution layer)**: the order-submission contract. The chosen
  `Executor` is built by `create_executor(config, execution_repository)` and
  exposes:
  ```python
  Executor.mode -> str
  Executor.execute(order: OrderRequest) -> ExecutionResult
  OrderRequest(symbol, transaction_type, quantity, order_type=MARKET,
               product=INTRADAY, exchange="NFO", entry_price=None,
               stop_loss=None, target=None, signal_id=None)
  ExecutionResult(status: ExecutionStatus, order, remarks, broker_order_id=None, timestamp)
  ```
  `OrderRequest`/`ExecutionResult` are broker-independent; `broker_order_id` is
  `None` for a dry run and carries the broker's id once a live executor submits.
  The mapping from `Signal` + `TradeDecision` → `OrderRequest` is
  `pipeline.build_order_request()`, which the `SignalPipeline` calls between the
  decision and the executor.

### Persistence schema — `signals`

| column | type | notes |
| --- | --- | --- |
| `id` | INTEGER | PK, autoincrement |
| `message_hash` | TEXT | deterministic content dedupe key (SHA-256 hex) |
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
| `trade_date` | TEXT | trading day (market-tz `YYYY-MM-DD`); **`UNIQUE(message_hash, trade_date)`** |

### Persistence schema — `executions`

Records each execution **attempt** (the order as it would be / was submitted, and
the verdict) — *not* market fills, positions, or P&L.

| column | type | notes |
| --- | --- | --- |
| `id` | INTEGER | PK, autoincrement |
| `signal_id` | INTEGER | FK → `signals(id)` (nullable) — the signal that produced the order |
| `timestamp` | TEXT | ISO-8601, when the attempt was made |
| `symbol` | TEXT | instrument as submitted |
| `action` | TEXT | BUY / SELL |
| `quantity` | INTEGER | order quantity |
| `order_type` | TEXT | MARKET / LIMIT |
| `entry_price` | REAL | entry / limit price (nullable) |
| `stop_loss` | REAL | nullable |
| `target` | REAL | nullable |
| `status` | TEXT | `ExecutionStatus` — SUCCESS / REJECTED / FAILED |
| `remarks` | TEXT | human-readable note (e.g. rejection reason) |

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

5. **Deterministic content hash for per-day deduplication** — `signal_hash()`
   hashes the *structured* fields (not `raw_text`) in a canonical order. Combined
   with a `trade_date` column, the uniqueness key is `UNIQUE(message_hash,
   trade_date)`: the same trade reposted with different whitespace/casing/chatter
   on the **same day** is a duplicate, but the same signal on a **later day** is
   accepted as a fresh trade (signals legitimately recur day to day). The
   "day" is the trading day in the configured market timezone (injected into the
   repository). Enforcing uniqueness as a DB constraint (not just an app check)
   makes the database the source of truth and is race-safe; the engine's
   duplicate rule asks the same question scoped to its clock's date.

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
    limit, lot size) is an independent `TradeRule` checked in order; the first
    rejection wins. Rules are config-driven and the rule set is injectable, so
    future risk rules slot in without modifying the engine. I/O-derived facts
    (now, today's count, duplicate?, lot size) are gathered once into an
    immutable `EvaluationContext`, keeping each rule a pure, trivially testable
    function.

12. **Lot sizing behind a provider seam** — options trade in *lots*, so the user
    configures `TRADE_LOTS` and the engine emits `quantity = lots × lot_size`
    (the unit count a broker order needs). Lot sizes are exchange-defined,
    per-underlying, and revised periodically, so they are never hardcoded: they
    come through a `LotSizeProvider` (`ConfigLotSizeProvider` from `.env` now).
    A `BrokerLotSizeProvider` reading the broker instrument master can replace it
    in Phase 5 without touching the engine. Missing lot size → clean rejection,
    never a guessed size.

13. **Three separated concerns: decide / execute / integrate** — the system keeps
    three responsibilities strictly apart so each can change without the others:
    - **Trade Engine** (`trade_engine.py`) — *should we act on this signal?* Pure
      decision logic; emits a `TradeDecision`. No order placement, no broker.
    - **Execution Layer** (`execution/`) — *submit the order.* Takes an
      `OrderRequest`, validates it, records the attempt, and (in live mode) sends
      it. Broker-independent: it knows nothing of Telegram, the parser, or any
      vendor SDK. The `Executor` interface is the seam.
    - **Broker Integration** — the *vendor-specific* code that actually talks to
      Kite/FYERS. It lives **inside a concrete `Executor`** (the future
      `KiteExecutor`) and nowhere else, so the broker SDK never leaks upward.

    This is why the engine has no broker knowledge and the execution layer has no
    Telegram knowledge — the boundaries are the design.

14. **`DryRunExecutor` validates the pipeline; `KiteExecutor` will replace it** —
    the strategy is external (the Telegram signals); this app's job is to execute
    signals *reliably*, not to simulate a market. So the first executor does not
    model fills, positions, or P&L (an earlier `PaperBroker` that did was removed).
    Instead `DryRunExecutor` validates an order, **logs exactly what would be
    submitted**, and records the attempt to SQLite — proving the whole path
    (engine → executor → history) works before any real order risk. Switching
    `EXECUTION_MODE` from `dry_run` to `kite` swaps in the live executor behind
    the identical `Executor` interface; nothing upstream changes. `DryRunExecutor`
    and `KiteExecutor` share the same `validate_order()` and `ExecutionResult`
    shape, so a request valid in a dry run is valid live.

## The execution layer

The `Executor` interface is the seam that keeps order submission broker-independent:
the Trade Engine decides, and *something behind this interface* executes — the
engine (and the listener) can never call Kite directly because they only ever see
the abstract `Executor`. **Broker integration lives inside a concrete executor**,
not above it.

```
        Trade Engine            (decides — teletrader.trade_engine)
            │  TradeDecision(execute, quantity, reason)
            ▼  (SignalPipeline maps Signal + decision → OrderRequest)
   ┌──────────────────────────────────────────────────────┐
   │                Executor  (abstract)                   │   teletrader.execution
   │  mode -> str                                          │
   │  execute(OrderRequest) -> ExecutionResult             │
   └──────────────────────────────────────────────────────┘
            ▲                                   ▲
            │  EXECUTION_MODE=dry_run            │  EXECUTION_MODE=kite (next phase)
     ┌────────────────┐                  ┌────────────────┐
     │ DryRunExecutor │                  │  KiteExecutor  │
     │ validate + log │                  │ submits to     │
     │ + record;      │                  │ Zerodha Kite   │  ← broker integration
     │ sends nothing  │                  │ (Kite Connect) │     lives HERE only
     └───────┬────────┘                  └────────────────┘
             │ ExecutionRepository
             ▼
        ┌──────────────────┐   executions (attempt + verdict; no fills/positions)
        │      SQLite       │
        │  teletrader.db    │
        └──────────────────┘
```

**Contract (`teletrader.execution`):**

```python
class Executor(ABC):
    @property
    def mode(self) -> str: ...
    def execute(self, order: OrderRequest) -> ExecutionResult: ...
```

- **Value objects** (frozen dataclasses, broker-independent): `OrderRequest`
  (`symbol, transaction_type, quantity, order_type, product, exchange,
  entry_price, stop_loss, target, signal_id`) and `ExecutionResult`
  (`status, order, remarks, broker_order_id, timestamp`).
- **Enums**: `ExecutionStatus` (SUCCESS / REJECTED / FAILED), `OrderType`
  (MARKET / LIMIT), `ProductType` (INTRADAY / MARGIN / DELIVERY),
  `TransactionType` (BUY / SELL).
- **Exceptions** — `except ExecutionError` catches the family: `ExecutionError` →
  `InvalidOrderError` (validation), `OrderRejectedError`,
  `BrokerCommunicationError` (the last two for the future live executor to
  translate broker failures into).
- **Validation** — `validate_order(order)` is the single shared gate (raises
  `InvalidOrderError`); both executors use it so behaviour is identical.
- **Selection** — `create_executor(config, repository)` returns the executor named
  by `EXECUTION_MODE`. This is the only place that maps the config string to a
  class; `kite` currently raises `NotImplementedError`.

### The `DryRunExecutor`

Validates → logs the would-be order → records the attempt. **No broker call, no
market simulation.** Example log for a valid order:

```
[DRY RUN]
BUY TCS
Quantity: 20
Order Type: MARKET
Entry: 3500
Stop Loss: 3450
Target: 3600
Result:
Order would have been submitted successfully.
```

A valid order yields `ExecutionResult(status=SUCCESS, …)`; an invalid one yields
`status=REJECTED` with the validation reason in `remarks`. Both outcomes are
logged and written to the `executions` table — `broker_order_id` is always `None`
because nothing was actually submitted.

### Execution sequence (wired)

The engine and the executor never meet directly — the `SignalPipeline` maps an
accepted `TradeDecision` (+ its `Signal`) to an `OrderRequest` and calls the
injected `Executor`. The listener owns only Telegram I/O; the engine stays a pure
decision-maker; the executor stays Telegram-unaware. The engine evaluates *before*
the signal is stored so it sees only prior history.

```
Telegram  Listener  Pipeline   Parser   TradeEngine  SignalRepo  Executor(dry_run)  ExecRepo
   │         │          │         │          │            │            │              │
   │ message │          │         │          │            │            │              │
   ├────────►│ process(text)      │          │            │            │              │
   │         ├─────────►│ parse_signal()─────►│ Signal     │            │              │
   │         │          │ evaluate(signal)───►│ TradeDecision           │              │
   │         │          │ add(signal)────────────────────►│ StoredSignal│              │
   │         │          │   (dup → DuplicateSignalError → DUPLICATE)     │              │
   │         │          │ if execute: build OrderRequest   │            │              │
   │         │          ├───────────────────────────────────────────────►│ execute()   │
   │         │          │                     │   validate + [DRY RUN] log │            │
   │         │          │                     │            │            ├─────────────►│ add()
   │         │          │◄──────────────────────────────────────────────┤ ExecutionResult
   │         │◄─────────┤ PipelineResult       │            │            │              │
   │  print (SIGNAL #id + SUCCESS|REJECTED / (duplicate) / (ignored))    │              │
```

Only the rightmost `Executor` is mode-specific; everything to its left is
unchanged whether the executor is dry-run or Kite. Swapping `EXECUTION_MODE` (and,
next phase, adding `KiteExecutor`) is the only difference between validating the
pipeline and trading live.

## Where future phases plug in

- **Phase 4 wiring — done**: the `SignalPipeline` drives the full live loop
  (*parse → evaluate → store → execute*); `main.py` builds the executor via
  `create_executor(config, execution_repository)` and injects the pipeline into the
  listener. Sending a Telegram signal now records a dry-run execution attempt.
- **Phase 5 (live broker) = `KiteExecutor`**: implement `Executor` against the
  Kite Connect SDK (auth, order placement, error translation into the
  `ExecutionError` hierarchy), returning an `ExecutionResult` with the real
  `broker_order_id`. Enable it with `EXECUTION_MODE=kite` — no change to the
  engine, listener, or models. A `BrokerLotSizeProvider` (from the broker
  instrument master) can likewise replace the config-backed lot sizes.
- **More risk rules**: append `TradeRule`s (position sizing, per-underlying
  caps, exposure limits) to the engine's rule set — no engine changes needed.
- **Notifications**: a thin sink subscribed to `ExecutionResult` / stored-signal
  events.
