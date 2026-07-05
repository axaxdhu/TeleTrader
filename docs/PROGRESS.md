# Project Progress

Tracks development status of TeleTrader against the phases in `CLAUDE.md`.

## Status

| Phase | Description                       | Status            |
| ----- | --------------------------------- | ----------------- |
| 1     | Telegram listener only            | ✅ Done & verified |
| 2     | Signal parser                     | ✅ Done (32 tests) |
| 3     | Database storage                  | ✅ Done (20 tests) |
| —     | Trade engine (decision layer)     | ✅ Done (15 tests) |
| 4     | Execution layer + live wiring     | ✅ Done (28 tests) |
| 5     | Live broker integration (KiteExecutor) | ✅ Done (22 tests) |
| —     | Trade-management commands (Phase 1: parse + state + dry-run) | ✅ Done |
| —     | Trade-management commands (Phase 2: live Kite — protection, fill, OCO) | ✅ Done (229 total) |
| —     | Second channel (parse-only) + per-channel switches | ✅ Done (253 total) |

> Per `CLAUDE.md`: do not implement later phases unless explicitly requested.
> The trade engine is the broker-agnostic decision layer (it *decides*, it does
> not *trade*); it feeds the **execution layer** (Phase 4), which *submits*
> orders. The strategy is external (the Telegram signals) — this app executes
> them; it does not simulate a market.
>
> **Plan change (2026-06-28):** an earlier iteration built a broker abstraction +
> a simulated `PaperBroker` (fills/positions/P&L). That was **removed** in favour
> of a leaner, broker-independent **execution layer** with interchangeable
> `DryRunExecutor` / future `KiteExecutor`. See the Execution layer notes below.

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
    column — DB is the source of truth, not just an app-level check. *(Updated
    2026-06-28 to per-day dedupe — see "Dedupe change" below.)*
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
| `trade_date`        | TEXT    | trading day; `UNIQUE(message_hash, trade_date)` (v4) |

### Project structure (updated)

```
main.py                         # config → connect/initialize → repos → listener
src/teletrader/
  config.py                     # + database_path (DATABASE_PATH env)
  database.py                   # NEW — connect() + migrations (user_version)
  repository.py                 # NEW — SignalRepository, StoredSignal, signal_hash
  parser.py                     # Phase 2 (unchanged)
  commands.py                   # NEW — ManagementCommand parser + parse_message()
  telegram_listener.py          # Telegram I/O only → SignalPipeline
  pipeline.py                   # SignalPipeline: parse→evaluate→store→execute / command→manage
  logging_config.py
  trade_engine.py               # decision layer
  trade_manager.py              # NEW — applies management commands to active trades
  trade_repository.py           # NEW — TradeRepository, StoredTrade (active-trade state)
  lot_size.py                   # LotSizeProvider seam
  execution/                    # order execution layer (broker-independent)
    __init__.py                 #   re-exports public surface
    base.py                     #   abstract Executor (+ cancel/modify mgmt ops)
    models.py                   #   OrderRequest, ExecutionResult, ManagementResult, enums
    validation.py               #   validate_order() (shared gate)
    dry_run.py                  #   DryRunExecutor
    kite.py                     #   KiteExecutor (live Zerodha Kite; lazy SDK)
    kite_instruments.py         #   KiteInstrumentResolver (symbol resolution)
    repository.py               #   ExecutionRepository, StoredExecution
    exceptions.py               #   ExecutionError hierarchy
    factory.py                  #   create_executor() — EXECUTION_MODE switch
tests/
  test_parser.py                # 32
  test_repository.py            # 20
  test_trade_engine.py          # 15
  test_executor.py              # 19 — DryRunExecutor + factory + validation + mgmt ops
  test_execution_repository.py  # 8  — executions table CRUD + schema
  test_pipeline.py              # 9  — end-to-end signals + management commands
  test_kite_executor.py         # 40 — KiteExecutor: orders, SL-M, status, cancel/modify, errors
  test_kite_instruments.py      # 9  — KiteInstrumentResolver (fixture data)
  test_commands.py              # 44 — ManagementCommand parser + parse_message dispatch
  test_trade_repository.py      # 14 — trades table CRUD + lifecycle + most_recent_active
  test_trade_manager.py         # 19 — command handlers + open_position + OCO reconcile
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

## Broker abstraction + paper broker — REMOVED (2026-06-28)

An earlier iteration (2026-06-27/28) built a broker abstraction layer
(`src/teletrader/broker/`: abstract `Broker`, `OrderRequest`/`OrderResponse`/
`Position`/`Balance`, `BrokerError` hierarchy) and a simulated `PaperBroker`
(market/limit fills, slippage, brokerage, netted positions, realised P&L,
`paper_orders`/`paper_positions` tables). **This was removed** at the user's
direction: the trading strategy is external (the Telegram signals), so the app's
job is to *execute* signals, not to *simulate a market*. It was replaced by the
execution layer below. (History kept here so the schema-version jumps make sense:
migrations v2/v3 created the paper tables; v5 drops them.)

## Phase 4 notes — execution layer (completed 2026-06-28)

A broker-independent **order execution layer**. The trade engine decides; an
`Executor` *submits* the order. Two interchangeable implementations sit behind one
interface, chosen by `EXECUTION_MODE`: `DryRunExecutor` (now) and a future
`KiteExecutor` (live). No market data, no virtual positions, no P&L — the dry run
only verifies the *correct order would have been sent*.

- New package `src/teletrader/execution/` (small, focused modules — SOLID):
  - `base.py` — abstract `Executor`: `mode` + `execute(OrderRequest) -> ExecutionResult`.
  - `models.py` — `OrderRequest` (`symbol, transaction_type, quantity, order_type,
    product, exchange, entry_price, stop_loss, target, signal_id`),
    `ExecutionResult` (`status, order, remarks, broker_order_id, timestamp`), and
    enums `ExecutionStatus` (SUCCESS/REJECTED/FAILED), `OrderType` (MARKET/LIMIT),
    `ProductType`, `TransactionType`. All frozen/slotted, broker-independent.
  - `validation.py` — `validate_order()`, the single shared gate (raises
    `InvalidOrderError`); both executors use it so a request valid dry is valid live.
  - `dry_run.py` — `DryRunExecutor`: validate → log the `[DRY RUN]` block of the
    would-be order → record the attempt. Sends nothing. Valid → `SUCCESS`
    ("Order would have been submitted successfully."); invalid → `REJECTED` with
    the reason. `broker_order_id` is always `None`. Deps injected
    (`ExecutionRepository`, optional `clock`).
  - `repository.py` — `ExecutionRepository` / `StoredExecution` over the new
    `executions` table (the `database`/`repository` split, mirrored).
  - `exceptions.py` — `ExecutionError` hierarchy: `InvalidOrderError` (validation),
    `OrderRejectedError`, `BrokerCommunicationError` (last two for the future live
    executor to translate broker failures into).
  - `factory.py` — `create_executor(config, repo)` returns the executor named by
    `EXECUTION_MODE`; `kite` raises `NotImplementedError` (next phase). The one
    place config maps to a class — switching modes is sufficient to switch executors.
- **Schema:** migration **v5** drops `paper_orders`/`paper_positions` and creates
  `executions` (id, signal_id FK→signals, timestamp, symbol, action, quantity,
  order_type, entry_price, stop_loss, target, status, remarks). `SCHEMA_VERSION`
  now **5**. Records execution *attempts*, not fills. Verified on the live DB:
  v3→v5 preserved all 7 signals, dropped the paper tables, created `executions`.
- **Config:** removed the four paper fields (and `_parse_float`); added
  `execution_mode` (`EXECUTION_MODE`, default `dry_run`, validated against
  {`dry_run`, `kite`}). `.env.example` updated.
- **Independence:** the layer imports nothing from Telegram and no broker SDK.
- Tests: `tests/test_executor.py` + `tests/test_execution_repository.py` cover
  valid order, invalid order, `[DRY RUN]` logging, execution-history persistence
  (incl. rejected attempts), config switching (dry_run ↔ kite-not-implemented ↔
  bad mode → `ConfigError`), and the exception hierarchy. `test_trade_engine.py`'s
  `_config()` swapped the paper fields for `execution_mode`. Total suite
  **90 passing** (`uv run pytest`).

## Pipeline wiring — live end-to-end (completed 2026-06-28)

The execution layer is now **wired into the live listener**. A real Telegram
message runs the full path: *parse → evaluate → store → execute (dry run)*.

- New `src/teletrader/pipeline.py` — `SignalPipeline.process(text, when=None)`
  orchestrates one message and returns a `PipelineResult`
  (`IGNORED` / `DUPLICATE` / `NOT_TRADED` / `EXECUTED`). Telethon-free, so it is
  unit-tested directly. Holds `build_order_request(signal, decision, signal_id)`
  — the seam mapping `Signal` + `TradeDecision` → `OrderRequest` (symbol e.g.
  `"NIFTY 23900 PE"`, `MARKET`, qty from the decision, entry/SL/target carried
  for the log/audit), keeping the decision and execution layers decoupled.
- **Ordering:** the engine evaluates **before** the signal is stored, so its
  duplicate/daily-count rules see only *prior* history (not the row being
  processed); `repository.add` is the authoritative same-day dedupe (a repeat →
  `DuplicateSignalError` → `DUPLICATE`). *(Storing first would make the engine
  flag every signal as a duplicate of itself — the bug caught while wiring.)*
- `telegram_listener.py` — now owns **only** Telegram I/O: hands each message's
  text to the pipeline and prints a one-line outcome. Constructor is
  `TelegramListener(config, pipeline)`. Exceptions in processing are caught so one
  bad message can't kill the listener. Listing/startup logs the execution mode.
- `main.py` — composition root now builds `TradeEngine`, `ExecutionRepository`,
  the executor (`create_executor`), and the `SignalPipeline`, and injects the
  pipeline into the listener.
- Tests: `tests/test_pipeline.py` (5) — ignored / executed / duplicate /
  not-traded (auto-trading off) outcomes over real collaborators + in-memory DB,
  plus the `build_order_request` mapping. Verified end-to-end: a valid signal logs
  the `[DRY RUN]` block and records one `executions` row linked to the signal.
  Total suite **95 passing** (`uv run pytest`).

## Dedupe change — per trading day (2026-06-28)

Duplicate detection is now **per day**: the same signal is rejected only if it
was already stored **on the same trading day**, and is accepted again on a later
day (signals legitimately recur day to day). Previously the same signal was
rejected forever.

- **Schema:** migration **v4** (`database.py`) adds a `trade_date` column and
  changes the uniqueness key from `UNIQUE(message_hash)` to
  `UNIQUE(message_hash, trade_date)`. SQLite can't drop a table-level UNIQUE, so
  the `signals` table is rebuilt; existing rows backfill `trade_date` from the
  date part of `created_at`. `SCHEMA_VERSION` now **4**. Verified on the live DB:
  v3→v4 preserved all 7 rows and backfilled dates.
- **Repository:** `SignalRepository(connection, *, tz=timezone.utc)` — the
  trading day is `created_at` seen in `tz`. `add()` stores `trade_date` and
  rejects only same-day repeats. `exists(signal, *, on_date=None)` is now
  day-scoped (defaults to today in `tz`). `signal_hash()` is unchanged (still
  pure content — its determinism tests stand).
- **Engine:** `DuplicateRule` is unchanged; `_build_context` now calls
  `exists(signal, on_date=now.date())` so "duplicate" means "seen today" by the
  engine's own clock.
- **Wiring:** `main.py` injects `tz=ZoneInfo(config.market_timezone)` so "day"
  is the trading day, not a UTC day. (During market hours IST and UTC dates
  coincide anyway; the tz matters only near midnight.)
- Tests: +4 — repository same-day-rejected / different-day-accepted /
  day-scoped `exists`, and an engine test that yesterday's signal is not a
  duplicate today. (For the current suite total, see the execution-layer notes.)

## Phase 5 notes — live Kite execution (completed 2026-06-28)

The live broker executor. `KiteExecutor` implements the **same** `Executor`
interface as `DryRunExecutor`; switching `EXECUTION_MODE` from `dry_run` to `kite`
is the only change needed to trade live — the engine, pipeline, listener, and
models are untouched and stay completely unaware of Kite.

- New `src/teletrader/execution/kite.py` — `KiteExecutor`:
  - **Manual auth.** Builds a `kiteconnect.KiteConnect` client at init from
    `KITE_API_KEY` + `KITE_ACCESS_TOKEN` and calls `set_access_token()`. It
    **never** logs in or generates a token (a valid daily token is assumed to
    exist). The SDK is imported **lazily** (only when a real client is built), so
    `dry_run` never loads the heavy `kiteconnect`/twisted stack and the module is
    importable without it.
  - **Tradingsymbol resolution** — a signal names an option only by
    underlying/strike/type (no expiry); Kite needs the exact `tradingsymbol`.
    `KiteInstrumentResolver` (`kite_instruments.py`) fetches Kite's NFO instrument
    master **once per trading day** (cached), indexes the option contracts, and
    returns the **nearest expiry on/after the order date** — the current weekly
    (nearest monthly for underlyings without weeklies; derived from the master, no
    hardcoded NSE rules). Returns `ResolvedInstrument(tradingsymbol, exchange,
    expiry, lot_size)`; no match → `InstrumentNotFoundError` → `REJECTED`. Injected
    into the executor (a fake resolver is injected in tests). The order date is the
    market-tz date (the executor takes `tz`, wired from `MARKET_TIMEZONE`).
  - **Structured fields on `OrderRequest`** — `underlying`/`strike`/`option_type`
    now travel alongside the readable `symbol` (`build_order_request` sets them) so
    the executor can resolve; `symbol` stays the display string for logs/dry-run.
  - **Order translation** (`_to_kite_params`) — the only Kite-specific mapping in
    the codebase: `variety="regular"`, `product` → `MIS`/`NRML`/`CNC`, the resolved
    `tradingsymbol`/`exchange`, a `price` for LIMIT orders;
    `transaction_type`/`order_type` pass through (neutral spellings already match
    Kite). No Kite type leaks out.
  - **Shared validation gate** first (`validate_order`), so a request valid dry is
    valid live; a validation failure → `REJECTED` before any broker call.
  - **Returns `ExecutionResult`**, never a raw Kite response — `SUCCESS` carries
    the real `broker_order_id`.
  - **Never crashes.** `execute()` catches everything and returns a structured
    result. `translate_broker_exception()` maps the SDK's native errors to the
    neutral hierarchy; `_status_for` picks the status:
    `REJECTED` for broker order rejections (incl. insufficient margin, market
    closed, invalid symbol), `FAILED` for token/network/rate-limit/unexpected.
  - **Logging** — one `[KITE]` line per attempt: timestamp, signal id, order,
    broker response, status, `broker_order_id`, and **execution duration**.
    Secrets are never logged.
- **Exceptions** (`exceptions.py`) — added `AuthenticationError`,
  `InsufficientMarginError(OrderRejectedError)`, `RateLimitError(BrokerCommunicationError)`,
  and `InstrumentNotFoundError(OrderRejectedError)` to the `ExecutionError` family.
  Clear, testable failure categories.
- **Config** — added `kite_api_key`, `kite_api_secret`, `kite_access_token`
  (`KITE_*` env). Required **only** when `EXECUTION_MODE=kite` — config load fails
  fast if any is missing; optional/ignored in `dry_run`. `.env.example` updated.
- **Factory** — `create_executor` now returns `KiteExecutor` for `kite` (passing
  the credentials); it no longer raises `NotImplementedError`.
- **Schema:** migration **v6** adds a `broker_order_id` column to `executions`
  (the live success path records the broker's id; `NULL` for dry run / non-success).
  `SCHEMA_VERSION` now **6**. `ExecutionRepository`/`StoredExecution` carry it.
  Verified: v1→v6 migrates cleanly and preserves existing rows.
- **Dependency:** `kiteconnect==5.2.0` added (`pyproject.toml` / `uv.lock`).
- **Daily token helper** (`kite_login.py`, project root) — Kite access tokens
  expire every morning (~6 AM IST). This standalone helper regenerates one: it
  prints the login URL, you log in in the browser, paste the `request_token` (or
  the whole redirect URL) back, and it exchanges it (with `KITE_API_SECRET`) and
  writes `KITE_ACCESS_TOKEN` into `.env`. Manual login only — no password/2FA is
  automated or stored. `--request-token` / `--env-file` flags for scripting.
  Since `KiteExecutor` builds its client at startup, refresh the token **before**
  starting the app each trading day. (Untracked-style dev helper, like
  `list_dialogs.py`; not on the runtime path.)
- Tests:
  - `tests/test_kite_executor.py` (27) — all with **mocked** Kite responses (fake
    client + fake resolver; no network): successful order, rejected order, invalid
    token, insufficient funds, market closed, invalid symbol, network timeout,
    network error, rate limit, unexpected exception; **resolution** (resolve called
    with the right args/date, market-tz date selection, unresolvable → `REJECTED`,
    missing option details → `REJECTED`, master-fetch failure → `FAILED`); the
    param translation (incl. product codes + LIMIT price + resolved tradingsymbol),
    persistence (incl. `broker_order_id`), `[KITE]` logging with duration,
    no-secrets-in-logs, missing-credentials fail, exception translation, and **two
    guards that the Trade Engine never sees Kite**.
  - `tests/test_kite_instruments.py` (9) — `KiteInstrumentResolver` over fixture
    data: nearest-weekly selection, past-expiry skip, expiry-day eligibility,
    CE/PE + strike + underlying isolation, not-found, **per-day caching** (one
    fetch/day, refetch on a new day), and tolerant parsing (datetime expiry, float
    strike, malformed rows).
  - `test_executor.py`'s kite test flipped from "not implemented" to "builds a
    `KiteExecutor`". Total suite now **131 passing** (`uv run pytest`).

**Known follow-up (out of scope here):** a `BrokerLotSizeProvider` can source lot
sizes from the instrument master (`KiteInstrumentResolver` already surfaces
`lot_size`) and replace the config-backed lot sizes behind the existing
`LotSizeProvider` seam — no engine change needed.

## Trade-management commands — Phase 1: parse + state + dry-run (completed 2026-06-30)

Beyond *entry* signals, the channel also posts short **management** messages that
act on an already-placed trade rather than open a new one. Three are supported,
from real channel wording:

| Message | Command | Semantics |
| --- | --- | --- |
| `Avoid` | `AVOID` | cancel the entry if placed but **not yet filled** |
| `SAFE TRADERS BOOK PROFIT` | `BOOK_PROFIT` | exit the open position now, at market |
| `MODIFY SL TO COST` | `MODIFY_STOP_LOSS` (to cost) | move the stop-loss to the entry price |

(`MODIFY_TARGET` is wired with a **tentative** regex — no real "move target"
message has been provided yet; confirm the wording before relying on it.)

This is **Phase 1** of the feature: parsing, active-trade state, and the full
command behaviour exercised on the **dry-run** executor. No live broker calls yet
(that is Phase 2 — see *Next*). Design decisions agreed up front: commands
correlate to a trade **positionally** (the most recently active trade — no
Telegram-reply linkage); the app will **manage real resting stop-loss orders** on
the broker (so "move SL" modifies a live order); all four command types are in
scope.

- **`commands.py`** — deterministic, regex-only command parser (mirrors
  `parser.py`). `CommandAction` enum, `PriceRef.COST` (symbolic "move to entry"),
  the frozen `ManagementCommand`, `parse_command()`, and **`parse_message()`** —
  the single entry point that returns a `Signal` (entry), a `ManagementCommand`,
  or `None` (noise). Patterns are **strict** (anchored full-message matches, edge
  punctuation/emoji and channel-branding prefixes tolerated): a false positive can
  exit a *real* position, so a miss is preferred to a guess. Entry signals (the
  fixed 3-line shape) are tried first and never collide with commands.
- **`trade_repository.py`** + migration **v7** — the `trades` table and
  `TradeRepository`/`StoredTrade` (mirrors the `database`/`repository` split).
  Tracks the live position through `PENDING_ENTRY → OPEN → CLOSED / CANCELLED`.
  Key query **`most_recent_active()`** (latest `PENDING_ENTRY`/`OPEN`; skips
  closed/cancelled) backs the positional correlation. Broker handles
  (`tradingsymbol`, `entry_order_id`, `sl_order_id`, `target_order_id`) are
  nullable — filled in by the live executor in Phase 2; a dry run leaves them
  `NULL`. `SCHEMA_VERSION` now **7**. Verified: v5→v7 on the live DB preserved all
  8 signals and created `trades`.
- **`trade_manager.py`** — `TradeManager` turns a `ManagementCommand` into the
  right broker action on the most-recent-active trade (the command analogue of
  `build_order_request`). **Avoid** → cancel a `PENDING_ENTRY` entry (else
  "already filled — use book profit"); **Book profit** → a market **SELL** through
  the ordinary `Executor.execute` (so it is validated + recorded like any order),
  then cancel the resting stop; **Move SL/target** → to *cost* (the trade's entry
  price) or an explicit number. Returns a `ManagementReport`
  (`ManagementOutcome`: `NO_TARGET` / `NOT_APPLICABLE` / `CANCELLED` / `EXITED` /
  `MODIFIED` / `FAILED`). Also owns `open_trade()` — the pipeline calls it after a
  successful entry execution.
- **Executor interface** (`execution/base.py`, `models.py`) — added broker
  operations on an *existing* order: `cancel_order`, `modify_stop_loss`,
  `modify_target`, returning a new broker-neutral `ManagementResult`
  (`ManagementAction` enum). The base provides **safe defaults** that report
  `FAILED` ("not supported in '<mode>' mode"), so `KiteExecutor` stays concrete
  and degrades cleanly until Phase 2 implements them. `DryRunExecutor` overrides
  them to log `[DRY RUN] Would CANCEL/MODIFY …` and report `SUCCESS`. (Booking
  profit is **not** a new op — it is an ordinary exit order via `execute`.)
- **`pipeline.py`** — `process()` now dispatches via `parse_message`: an entry
  signal runs the existing path **and opens an active `trades` row on a successful
  execution**; a command is applied via the `TradeManager`. New `PipelineStatus`
  members (`NO_TARGET` / `NOT_APPLICABLE` / `CANCELLED` / `EXITED` / `MODIFIED` /
  `MGMT_FAILED`); `PipelineResult` carries the `command` + `ManagementReport`.
  `SignalPipeline(repository, engine, executor, trade_manager)`.
- **`telegram_listener.py`** — prints a `COMMAND … → outcome` line. With positional
  correlation the listener needs **no** extra message metadata, so its signature is
  unchanged.
- **`main.py`** — builds `TradeRepository` + `TradeManager` and injects the manager
  into the pipeline.
- **Dry-run simplification (Phase 1 only):** a dry run can't observe fills, so a
  successfully *submitted* entry opens the trade directly as **`OPEN`** (with the
  signal's levels; `entry_price` is the "cost" a later *move SL to cost* uses).
  This lets book-profit and stop moves be exercised end-to-end; `Avoid` (which
  needs `PENDING_ENTRY`) reports "already filled" in a pure dry-run flow, and its
  cancel path is covered by manager unit tests with a pending trade. Phase 2 (live)
  refines this to `PENDING_ENTRY → OPEN`-on-fill.
- Tests: `test_commands.py` (44), `test_trade_repository.py` (14),
  `test_trade_manager.py` (11), plus management cases added to `test_pipeline.py`
  and `test_executor.py`. Verified end-to-end on a dry-run pipeline: entry →
  `MODIFY SL TO COST` (SL → 165) → `BOOK PROFIT` (exited) → `Avoid` (no target).
  Total suite **208 passing** (`uv run pytest`).

## Trade-management commands — Phase 2: live Kite (completed 2026-06-30)

Makes the management commands act on the **real broker**, and — crucially — makes
the system place the **protective orders** it never placed before. Today an entry
goes to Kite with no stop; this phase adds a resting **stop-loss (SL-M)** and a
resting **target (LIMIT)** after the entry fills, both **MIS**, plus app-managed
**one-cancels-other (OCO)**.

**Why this shape** (decisions made with the user): the channel does **not** always
send exit messages, so *both* protective legs must be automated — relying on a
"BOOK PROFIT" message isn't safe. Zerodha discontinued **Bracket Orders** (2021),
so there's no single entry-with-stop order — the stop must be a **separate order**,
and it can only be placed sensibly *after* the entry fills. **GTT-OCO was rejected**
because GTT places **NRML/CNC**, not MIS, so it wouldn't cleanly flatten an intraday
MIS long. So: two separate MIS exit orders (SL-M + LIMIT), with the app handling the
OCO via Kite's reliable `cancel_order`. Residual risk: a small double-fill race if
price gaps through both levels before the sibling is cancelled (documented; bounded
by reconcile latency).

- **Architecture choice — `execute()` stays a single-order primitive.** Rather than
  bundling fill-polling + child orders into `KiteExecutor.execute()` (which would
  break its "place one order" contract and tests), the broker-specific calls stay
  one-order each, and the **protected-entry orchestration lives in the broker-
  agnostic `TradeManager`**. This keeps broker code inside the executor and
  sequencing outside it, and the dry run gets protective placement for free.
- **New execution primitives** (`execution/models.py`, `base.py`, `kite.py`):
  - `OrderType.SL_M` + `OrderRequest.trigger_price` — the resting stop; `validate_order`
    requires a positive trigger. `_to_kite_params` emits `trigger_price` for SL-M
    (and the existing `price` for LIMIT) — the only Kite-specific mapping, unchanged
    for the entry.
  - `OrderState` / `OrderStatus` (PENDING / COMPLETE / CANCELLED / REJECTED /
    UNKNOWN) + `Executor.get_order_state(broker_order_id)` — used for fill detection
    and OCO. Base default is `UNKNOWN` (safe — never treats anything as filled);
    `DryRunExecutor` reports `COMPLETE` (so orchestration runs end-to-end with no
    broker ids); `KiteExecutor` reads `order_history` and maps Kite's status string.
  - `KiteExecutor.cancel_order` / `modify_stop_loss` / `modify_target` against the
    SDK (`cancel_order` / `modify_order` with `variety="regular"`), every failure
    translated to a structured `ManagementResult` (never raises). The `KiteClient`
    Protocol gained `modify_order` / `cancel_order` / `order_history`.
- **`TradeManager.open_position(signal, entry_order, …)`** — the orchestration:
  place entry → **bounded poll** `get_order_state` until COMPLETE (configurable
  `fill_timeout` / `poll_interval`, injected `sleep`) → on a fill, place SL-M
  (trigger = signal stop) + target LIMIT (price = signal target), sized to the
  **actual filled qty**, recording all three order ids and the **fill price** on the
  trade (`OPEN`). An accepted-but-unfilled entry opens `PENDING_ENTRY` (still
  *Avoid*-able) with no protection; a rejected entry opens nothing. A failed
  protective-leg placement is logged loudly but doesn't abort (the entry is live).
- **`TradeManager.reconcile()`** — the OCO: for each `OPEN` trade with broker ids,
  if a leg filled, **cancel its sibling and close the trade**. Called by the
  pipeline before handling each message, so commands act on fresh state. Trades with
  no order ids (dry run) are skipped. (`Avoid` now works live: a `PENDING_ENTRY`
  entry is cancelled via `cancel_order`.)
- **Pipeline** — `_process_signal` now calls `trade_manager.open_position` (instead
  of `executor.execute` + `open_trade`); `process()` calls `reconcile()` first. The
  pipeline no longer holds the executor directly (the manager owns it):
  `SignalPipeline(repository, engine, trade_manager)`.
- **Dry run is unchanged in spirit but richer:** a successful entry now also logs +
  records the two protective orders (so the `executions` table shows entry + SL-M +
  LIMIT), and the trade opens `OPEN` with `NULL` order ids.
- Verified end-to-end (integration smoke, fake Kite client): entry placed → filled
  @164.5 → SL-M + target LIMIT placed → trade `OPEN`; then a stop fill drove
  `reconcile` to **cancel the target and close the trade**.
- Tests: +21 — `test_kite_executor.py` (SL-M params, `get_order_state` mapping +
  safety, cancel/modify); `test_trade_manager.py` (`open_position` filled/unfilled/
  rejected, live Avoid, OCO reconcile both directions + skip cases). Suite
  **229 passing** (`uv run pytest`).

**Known limitations / follow-ups:**
- **Reconcile cadence:** OCO fires on the *next inbound message*, so a self-triggered
  stop/target is cancelled with up-to-next-message latency (and `reconcile` calls
  `order_history` per open trade per message — watch Kite rate limits on a chatty
  channel). A websocket **postback**/ticker or a throttled periodic poll would make
  it prompt and cheaper.
- **Pending→open upgrade:** an entry that fills *after* the poll window stays
  `PENDING_ENTRY` and never gets protection retroactively — `reconcile` could be
  extended to detect the late fill and place the stops.
- **Fill-poll timing** is constructor config (defaults 5 s / 0.5 s), not `.env` —
  promote to `Config` if it needs operational tuning.
- **EOD square-off:** MIS auto-square-off (~15:20) isn't reconciled — stale resting
  orders/trades at day end should be cleaned up.
- **`MODIFY_TARGET`** parser wording is still tentative (no real message).

## Second channel — parse-only + per-channel switches (completed 2026-07-05)

Added a **second signal channel** (`-1001524695283`) alongside the primary one.
It posts the same *intent* (option entries) in a **very different, looser format**,
and — per decision — is **parse-only for now** (parsed + stored + logged, **not
traded**; live trading, likely via **FYERS**, comes later).

- **Channel-2 format** (drove a separate parser): variable field order (`Lot` /
  `Target` / `Sl` / `Cmp` matched **by label**, not line position); **any
  underlying**, mostly **stock** options (multi-word names like `Apollo hospital`),
  not just NIFTY/BANKNIFTY; **multiple comma-separated targets**; decimals; free
  chatter lines (`One more`, `Cmp …`). `Type mistake` = cancel the previous signal
  (ch2's *Avoid*); `🔥 … Safe target Done ✔️` / `sl hit on this` = narration (noise).
- **Decisions (2026-07-05):** parse-only for now; use the **first** target, taken
  **raw** (no offset — ch1's −2pt offset is wrong for cheap stock-option premiums);
  **single lot** everywhere (in-message `Lot` is ignored for sizing); per-channel
  **enable switch** via env at startup; **per-channel isolation** (storage tagged by
  source; trade-level isolation lands when ch2 trades).
- **`channel2.py`** — `parse_channel2_signal()`: deterministic regex, reuses the
  existing `Signal` shape. Header `^<underlying> <strike> <ce|pe> (above|at) <entry>$`
  (multi-word underlying, `at`-range takes the first number); `Sl`/`Target` scanned
  from any line (stray leading `.` tolerated). Needs header + numeric `Sl` + ≥1
  `Target` or it rejects (a miss beats a bad parse). First target used, raw;
  trailing `+`/`++` → `target_open_ended`.
- **`Channel2Pipeline`** (`pipeline.py`) — parse → store → log; **no engine /
  executor / trade manager**. Reuses `PipelineResult` with a new
  `PipelineStatus.STORED`. Same-day repeats → `DUPLICATE`; noise → `IGNORED`.
- **Source-scoped storage** — migration **v8** adds a `source` column to `signals`
  (plain `ALTER`; a table rebuild would break the `executions`/`trades` FKs; existing
  rows backfill to `channel1`). `SignalRepository(connection, *, tz, source=…)` now
  stores/queries only its source, so ch1's engine counts/dedupe never see ch2 rows.
  `SCHEMA_VERSION` now **8**. Verified on the live DB: v7→v8 preserved all 8 signals,
  backfilled `channel1`, FK check clean. *(Caveat: the day-scoped
  `UNIQUE(message_hash, trade_date)` is channel-agnostic — two channels posting an
  identically-hashing signal the same day would collide; vanishingly rare given the
  different instruments/formats, and harmless for a parse-only channel.)*
- **Multi-channel listener** — `TelegramListener(config, subscriptions)` takes a list
  of `ChannelSubscription(name, channel, processor)` and registers **one Telethon
  handler per channel** (Telethon does the routing; no manual `chat_id` dispatch).
  Both pipelines satisfy a shared `MessageProcessor` protocol, so the listener treats
  them uniformly. One process, one session — no second login.
- **Config** — `TELEGRAM_CHANNEL_2` (optional; unset ⇒ single channel),
  `CHANNEL_1_ENABLED` / `CHANNEL_2_ENABLED` (default **true**; a channel with no id
  is skipped). New trailing `Config` fields have defaults so existing constructors
  (and the test `_config()` helpers) are untouched. `.env.example` updated.
- **`main.py`** — builds a channel-2 `SignalRepository(source="channel2")` +
  `Channel2Pipeline`, assembles the enabled subscriptions, and errors out cleanly if
  none are enabled.
- **`fetch_history.py`** (dev helper) — gained `--raw` (dump full untruncated text,
  no parsing) and `--channel` numeric-id coercion, used to reverse-engineer ch2's
  format from real history.
- Tests: `test_channel2.py` (parser: standard/reordered/multi-word/decimals/
  first-target-raw/open-ended/`at`-range/`.lot`/lead-in + noise & missing-field
  rejection) and `test_channel2_pipeline.py` (ignored/stored/duplicate + source
  isolation). Suite **253 passing** (`uv run pytest`).

**Follow-ups when ch2 goes live (FYERS):** a channel-2 command parser (`Type mistake`
→ cancel) and **trade-level** per-channel isolation (tag `trades` with source so
management commands act only on same-channel positions); a FYERS `Executor`
implementation; stock-option symbol/lot resolution (verify the resolver handles
monthly expiries); decide scale-out vs first-target-only for real orders.

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
3. `uv run pytest` to confirm the 229 tests pass.
4. Outstanding housekeeping: optionally set a real `TELEGRAM_CHANNEL` (currently
   `me`); no git remote configured yet.

## Next

All phases (1–5) are complete and **wired end-to-end**. A live signal runs
parse→evaluate→store→execute, and the executor is selected by `EXECUTION_MODE`:
`dry_run` (validate + log, sends nothing) or `kite` (live Zerodha order).

**To trade live:** set `EXECUTION_MODE=kite` and supply `KITE_API_KEY`,
`KITE_API_SECRET`, `KITE_ACCESS_TOKEN` (generate the daily access token yourself —
the app does not log in). Also `AUTO_TRADING=true`, inside market hours. The
engine gates everything before the executor, so off-hours / auto-trading-off →
`NOT_TRADED` and nothing reaches Kite.

Live orders now resolve to the exact Kite tradingsymbol (nearest weekly) via
`KiteInstrumentResolver` — no manual symbol mapping needed.

**Known follow-up (not started):**
- **Trade-management Phase 2 (live Kite): ✅ done** — see the "Phase 2: live Kite"
  notes above (SL-M + target LIMIT protection, bounded fill poll, app-managed OCO,
  live cancel/modify). Remaining polish is listed there (reconcile cadence/postbacks,
  pending→open upgrade, EOD square-off, fill-poll config, real `MODIFY_TARGET`
  wording).
- **`BrokerLotSizeProvider`** — source lot sizes from the broker instrument master
  instead of `.env`, behind the existing `LotSizeProvider` seam.
  `KiteInstrumentResolver` already surfaces `lot_size`, so this is mostly wiring
  (and sharing the cached instrument dump) — no engine change.
