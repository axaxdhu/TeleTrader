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
| —     | FYERS broker + per-channel broker selection | ✅ Done (308 total) |
| —     | Telegram bot notifications (signal + outcome) | ✅ Done (322 total) |
| —     | Channel 2 shadow mode (FYERS payload, no order) + broker lot sizes | ✅ Done (424 total) |
| —     | End-of-day shadow P&L summary (would the day have been profitable?) | ✅ Done (463 total) |

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
management commands act only on same-channel positions); ~~a FYERS `Executor`
implementation~~ (✅ done — see below); ~~stock-option symbol/lot resolution~~
(✅ the resolver handles monthly expiries); decide scale-out vs first-target-only
for real orders.

## FYERS broker + per-channel broker selection (completed 2026-07-12)

Added a **second live broker (FYERS)** behind the existing `Executor` seam, and
made the broker **selectable per channel**, so different channels can trade
through different brokers (e.g. ch1 → Kite, ch2 → FYERS) — the goal being to
switch brokers at will. Nothing upstream (Trade Engine, pipeline, listener,
models) changed: this is the payoff of the broker-agnostic design.

**Decisions (2026-07-12, with the user):** per-channel broker via
`CHANNEL_N_BROKER` (naming confirmed); FYERS must handle **both index and stock
options** depending on channel; product type **INTRADAY** (matches Kite MIS + the
SL-M/target/OCO logic); credentials are **global per broker**, only the routing is
per-channel. See the `per-channel-broker` memory.

- **`execution/fyers.py`** — `FyersExecutor`, the FYERS twin of `KiteExecutor`
  behind the same `Executor` interface. Key difference from Kite: FYERS reports
  business errors in the **response dict** (`{"s": "error", …}`) rather than
  raising, so both a bad response *and* a raised transport error are translated to
  the neutral `ExecutionError` hierarchy (`_error_from_response` +
  `translate_fyers_exception`). `_to_fyers_params` is the only FYERS-specific
  mapping (numeric `side`/`type` codes, `productType`, `limitPrice`/`stopPrice`).
  Implements orders, SL-M, `get_order_state` (FYERS status ints → `OrderStatus`),
  `cancel_order`/`modify_stop_loss`/`modify_target`. Never crashes; logs one
  `[FYERS]` line per attempt with duration; records to `executions`. SDK
  (`fyers-apiv3`) imported **lazily** — the module is importable without it and
  dry-run/other brokers never pay its (heavy) import cost.
- **`execution/fyers_instruments.py`** — `FyersInstrumentResolver` +
  `FyersCsvSymbolMaster`. Unlike Kite (instrument dump via API), FYERS publishes
  its master as **downloadable CSV** (`NSE_FO.csv`); the master source downloads +
  parses it (cached per trading day, like Kite). Returns the master's own exact
  symbol ticker (e.g. `NSE:NIFTY2570323900PE`), sidestepping FYERS's differing
  weekly/monthly symbol formats. The **nearest-expiry-on/after-date** rule works
  for index (weekly) *and* stock (monthly) options with no special-casing. Reuses
  the shared `ResolvedInstrument` / `InstrumentResolver` seam. **⚠️ The CSV column
  indices (centralised as constants) are FYERS's documented layout but the format
  is unversioned — VERIFY against a live download before trading.**
- **`fyers_login.py`** (project root) — daily-token helper mirroring
  `kite_login.py`, for the FYERS auth-code flow (`SessionModel.generate_authcode`
  → browser login → capture `auth_code` on the local callback → `set_token` +
  `generate_token` → write `FYERS_ACCESS_TOKEN` into `.env`). Manual login only;
  no password/2FA automated. Redirect URI defaults to `http://localhost:8765`.
- **Config** (`config.py`) — `fyers` added to the valid modes; `FYERS_APP_ID` /
  `FYERS_SECRET_ID` / `FYERS_ACCESS_TOKEN` (required only when a channel uses the
  fyers broker; fail-fast). **Per-channel routing:** `CHANNEL_1_BROKER` /
  `CHANNEL_2_BROKER` (optional; fall back to `EXECUTION_MODE`); resolved via
  `Config.broker_for(channel)`. Credentials are required for **any broker
  referenced** (global default or a per-channel override).
- **Factory** (`execution/factory.py`) — `create_executor(config, repo, *,
  mode=…)` now takes an explicit mode (defaulting to `execution_mode`) and has a
  `fyers` branch, so each channel builds its own executor from its broker.
- **`main.py`** — builds channel 1's executor from `config.broker_for("channel1")`
  and logs it. (Channel 2 stays **parse-only** for now per the locked plan; the
  per-channel machinery is ready for when ch2 trades — it just needs a trading
  pipeline, ch2 command parser, and trade-level source isolation.)
- **Dependency:** `fyers-apiv3==3.1.14` added (`pyproject.toml` / `uv.lock`).
- **Live-trading prerequisite (operational, not code):** FYERS order placement is
  only accepted from a **whitelisted static IP** (SEBI/NSE retail-algo rule) set in
  the FYERS app. Plan is to run on a fixed-IP cloud host (DigitalOcean BLR1
  droplet) and whitelist its IP. Development/tests/dry-run need none of this.
- Tests: `test_fyers_executor.py` (~37 — mocked client + fake resolver: success,
  response-dict rejections/margin/market-closed/no-id, raised timeout/rate-limit/
  unexpected, resolution + market-tz date, numeric param translation, SL-M,
  order-state mapping incl. safety, cancel/modify, persistence, `[FYERS]` logging,
  no-secrets, missing-creds, error translation, engine-has-no-fyers guard, lazy
  SDK guard); `test_fyers_instruments.py` (13 — nearest-expiry for weekly *and*
  monthly, CE/PE + strike + underlying isolation, not-found, per-day caching,
  tolerant date/datetime/epoch parsing, CSV column mapping); plus factory + per-
  channel broker config tests in `test_executor.py`. Suite **308 passing**
  (`uv run pytest`).

**To trade a channel on FYERS:** set that channel's broker (e.g.
`CHANNEL_1_BROKER=fyers`), supply `FYERS_APP_ID`/`FYERS_SECRET_ID`/
`FYERS_ACCESS_TOKEN` (regenerate the token daily with `fyers_login.py`),
`AUTO_TRADING=true`, run from the whitelisted static IP, inside market hours.

## Telegram bot notifications (completed 2026-07-12)

Push alerts for **every recognised signal + its outcome** (the `Notifications`
box in the architecture), delivered via a Telegram **bot** — chosen over messaging
the user's own account because Telegram push-notifies for a bot's messages but not
for one's own. Fires on placed/dry-run orders, not-traded (with reason),
duplicates, parse-only stores, management-command results, and failures; pure
noise (`IGNORED`) is skipped. **Requested: alerts even when no order is placed and
in dry-run** — both covered.

- **`notifier.py`** — `Notifier` protocol, `NullNotifier` (no-op), and
  `TelegramBotNotifier` (one stdlib-`urllib` HTTPS POST to the Bot API; the sender
  is injectable for tests). **Best-effort:** any send failure is logged and
  swallowed, never raised, so a notification problem can't disturb trading.
  `create_notifier(config)` returns the bot notifier only when enabled + fully
  configured, else the no-op. `format_alert(result, *, channel_name, broker)`
  turns a `PipelineResult` into the alert text (or `None` to skip) — the listener
  stays thin.
- **`telegram_listener.py`** — takes an optional `notifier` (defaults to
  `NullNotifier`); after `_report` it formats + sends an alert for the message's
  outcome, tagged with the channel and its broker. No new Telethon coupling.
- **`main.py`** — builds the notifier via `create_notifier` and injects it; logs
  whether notifications are on.
- **Config** — `NOTIFY_ENABLED` (default false), `NOTIFY_BOT_TOKEN`,
  `NOTIFY_CHAT_ID` (trailing fields with defaults; the token is a secret, never
  logged). `.env.example` + README updated.
- **Independence:** the bot is a *separate identity* with no access to the user's
  account, signal channels, or broker/credentials — purely an outbound notifier.
- Tests: `test_notifier.py` (14 — formatting for each outcome incl. dry-run &
  not-traded, best-effort swallow of send failures, enabled/disabled/unconfigured
  factory). Suite **322 passing** (`uv run pytest`).

## Channel 2 shadow mode — the live FYERS path, no order (completed 2026-09-27)

**Why:** channel 2 (`-1001524695283`, SafeTraders premium) gives better and more
consistent signals than channel 1, so it is the channel to actually trade — and
the user has a full-time job, so it has to run unattended. Before real money, the
ask was to see, for every signal, the **actual broker-ready payload**, to know the
trade *would* have gone through. `dry_run` cannot answer that: it never contacts a
broker, so it proves the order's shape and nothing about whether FYERS would
accept the symbol, expiry, lot size or cost.

**Decisions (2026-09-27):** full broker check but no order; full payload pushed to
the Telegram bot; alert only on *signal-like* parse misses (not all chatter).

- **`execution/shadow.py` — `FyersShadowExecutor` (mode `fyers_shadow`)**
  Subclasses `FyersExecutor`, so it is the live path minus the last call: same
  validation, same `_resolve`, same `_to_fyers_params`. `FyersExecutor.execute`
  was refactored to extract **`_prepare()`** (validate → resolve → build params),
  which both share — so what shadow reports is the *real* payload, not a
  reconstruction. It then runs a **funds check** (option buying costs the full
  premium: `entry x qty`, compared against the FYERS `funds()` available balance)
  and stops. `SUCCESS` = would go through; `REJECTED` = would not (bad symbol,
  failed validation, or insufficient funds). An unreadable balance reports
  **unknown**, never a silent pass.
  Tests assert the guarantee directly: the injected client **raises** on
  `place_order`/`modify_order`/`cancel_order`, so any regression that submits
  fails the suite.
- **`ShadowReport`** (`models.py`) — resolved symbol, exchange, expiry, lot size,
  lots, quantity, the payload dict, and the funds verdict; attached to
  `ExecutionResult.shadow` (trailing optional field, `None` for every other
  executor). The `executions` table stores the *verdict*, not the raw request, so
  the contract/expiry/qty travel in `remarks` (persisted); the full payload goes
  to the log line.
- **`BrokerLotSizeProvider`** (`lot_size.py`) — **on the critical path, not
  polish**: ch2 trades stock options and `LOT_SIZES` only listed NIFTY/BANKNIFTY,
  so every ch2 signal would have been rejected as unsized. Reads lot sizes from
  the same FYERS master that resolves the symbol (exposed via the new
  `FyersExecutor.lot_size_source`), falling back to `ConfigLotSizeProvider` when
  the master is unreachable. A network failure logs and falls back rather than
  taking sizing down.
- **Underlying matching** (`fyers_instruments.py`) — signals say `Apollo
  hospital`, the master says `APOLLOHOSP`. Three deterministic steps: normalise
  case/spacing/punctuation (`L&T` → `LT`), apply a small **alias table** for names
  no rule can bridge (`BAJAJ FINANCE` → `BAJFINANCE`, `ULTRATECH` → `ULTRACEMCO`,
  `SBI` → `SBIN`), then accept a ticker that abbreviates the spoken name (one a
  prefix of the other) **only when exactly one** candidate matches. An ambiguous
  name (`Apollo` → APOLLOHOSP + APOLLOTYRE) **raises** rather than guessing —
  buying the wrong company is far worse than reporting an unclear name. Also adds
  `lot_size_for(underlying, *, on_date)`.
  **Validated against 136 real channel-2 signals** (the live droplet database,
  13 Jul - 25 Sep 2026, 70 distinct underlyings): **98% of signals resolve**
  (133/136). Two fixes came out of that run — the master's own keys are now
  normalised too (it spells some tickers with punctuation: `GVT&D`,
  `BAJAJ-AUTO`, which nothing could previously match), and aliases were added for
  the spellings the channel actually uses (`Tin india`, `Kpitech`, `Hyndai`,
  `Britania`, `Atherengg`, `Airtel`). The three remaining misses are correct
  rejections: `NUVAMA` and `EXIDE` have no contract in the master (NSE revises
  the F&O list), and `ONE MORE` was a parser bug, now fixed.
  **Also verified against the live master** (81,174 contracts, 216 underlyings):
  all 20 real-world stock names tried resolve correctly. `Tata motors` needed an
  alias of its own — post-demerger the listed F&O entity is **TMPV** (Tata Motors
  Passenger Vehicles, lot 1600), and it is the only Tata Motors contract in the
  master, so the mapping is unambiguous. The alias table is expected to keep
  growing as shadow alerts reveal new names.
- **Exchange from the symbol** — the master's exchange column is a numeric code
  (`10`); the tradingsymbol already carries the real one (`NSE:...`), so that is
  used instead and alerts no longer read "(10)".
- **`Channel2Pipeline` is no longer parse-only** — now *parse → store → evaluate →
  shadow-execute*, with the engine's real gates (market hours, dedupe, daily cap)
  applying, so a shadow report reflects what would really have happened. New
  `PipelineStatus.SHADOWED` is reported **only** when the executor actually
  withheld the order (a live broker reports `EXECUTED`), so the status can never
  overstate or understate what happened to the money. The trade state machine and
  management commands are deliberately still absent: with no order placed there is
  nothing to manage. Passing no engine/executor keeps the old parse-only shape.
- **Lead-in bug found in real history** — the channel posts `One more 23300 ce
  above 131`, chatter running straight into the header with **no underlying at
  all**; the parser credited it to an instrument named `ONE MORE` (one such row is
  in the live database). A lead-in prefix is now stripped before the header is
  matched, and such a message is **rejected** rather than parsed: the lot size
  (65) marks it as NIFTY, but inferring an instrument from a lot size is how you
  buy the wrong thing. It surfaces as a MISSED alert instead.
- **Missed-signal detection** — `looks_like_signal()` (`channel2.py`) flags a
  message carrying an option contract or an `Sl`/`Target` label that the parser
  rejected; narration (`target done`, `sl hit`, `type mistake`, `cmp <n>`) is
  excluded. Surfaces as `PipelineStatus.MISSED` → a console line and a
  "POSSIBLE SIGNAL NOT PARSED" Telegram alert **quoting the raw text**. Loose
  heuristic on purpose: a false alert costs a glance, a missed signal costs a trade.
- **Protective exits in the shadow report** (2026-09-28) — the entry is only half
  the plan: the live path places a resting SELL **SL-M** at the stop and a SELL
  **LIMIT** at the target *after* the entry fills, as separate orders (FYERS
  attaches neither to a market entry). Shadow mode now builds and validates both
  (`ShadowLeg`, `ShadowReport.protective`), mirroring
  `TradeManager._protective_order` — it needs no `Signal`, since the entry
  `OrderRequest` already carries stop/target/underlying. The alert lists each leg
  with its own ✅/❌, and a signal whose stop cannot be placed reports
  **"⚠️ ENTRY OK — PROTECTION INCOMPLETE"** rather than a plain "would go
  through": a position that opens and then cannot be closed on plan is not a
  working trade. Sizing note: the live path sizes these to the *actual* fill;
  with nothing filled the requested quantity is used and the report says so.
- **Alerts** (`notifier.py`) — `_format_shadow` renders symbol, exchange, expiry,
  side/type, `qty (lots x lot size)`, entry/SL/target, cost vs. available funds,
  and the verdict; the missed alert quotes the original message (trimmed).
- **Safety rail** (`main.py`) — channel 2 accepts only `dry_run` / `fyers_shadow`;
  any live broker exits at startup with an explanation, because ch2 has no trade
  manager and would place entries with **no protective stop-loss or target**.
- **Config** — `fyers_shadow` added to `_EXECUTION_MODES`. It deliberately does
  **not** require FYERS credentials (only the live `fyers` broker does): shadow
  mode places nothing, and the symbol master is a public file, so it runs with no
  account at all — the contract, expiry, lot size and payload are still real and
  only the funds check reports "unavailable". Requiring a token would block the
  very thing shadow mode exists for, namely seeing the real payload *before* the
  broker is set up. Discovered on the live droplet 2026-09-28: the `FYERS_*` keys
  were present in `.env` but **empty** (copied from `.env.example` on 12 July,
  never filled), so the first `fyers_shadow` start failed config validation and
  systemd crash-looped until the setting was reverted.
- Tests: `test_shadow_executor.py` (22), `test_lot_size.py` (11), plus additions to
  `test_channel2.py`, `test_channel2_pipeline.py`, `test_fyers_instruments.py`,
  `test_notifier.py`, `test_executor.py`. Suite **424 passing** (`uv run pytest`).

**Still open before ch2 goes live (`fyers`):** the trade manager must cover ch2
(protective SL-M + target with OCO, fill polling) — without it a live entry is
unprotected; **trade-level source isolation** (tag `trades` with their channel so
management commands act only on same-channel positions); a **ch2 command parser**
(`Type mistake` → cancel); and a decision on **scale-out vs first-target-only**.

## End-of-day shadow P&L (completed 2026-09-28)

Shadow mode answers "would this order have been accepted?". This answers the
question that actually decides whether the channel is worth trading: **would
following it have made money?** After the close, every shadowed trade is replayed
against its own contract's intraday candles and the day is totalled into one
Telegram summary.

```
📊 channel2 — 28 Sep 2026 (shadow)
3 signal(s) · 1 target · 1 stopped · 1 open

  ✅ COFORGE +1,425  (70 → 73, target)
  ❌ NIFTY -715  (131 → 120, stopped)
  ⏳ APOLLOHOSP +562  (307 → 311.50, open)

Gross: +1,272 on 80,140 deployed

Assumes entry filled at the signal's price; a bar spanning both stop and target
counts as stopped; gross of brokerage and taxes.
```

- **Migration v9 — `shadow_runs`.** `executions` stores a verdict and a prose
  remark: enough to audit an attempt, not enough to *price* one. The resolved
  contract, quantity and the three prices now survive as columns in their own
  table (only shadow runs have them, and a day's report is one indexed scan).
  `outcome`/`exit_price`/`pnl` stay NULL until scored, so "not yet judged" is
  never mistaken for "broke even". `SCHEMA_VERSION` now **9**.
- **`shadow_repository.py`** — `ShadowRun` / `StoredShadowRun` / `Outcome`
  (`target` / `stopped` / `open` / `unknown`), day-scoped in the market timezone
  so a report never straddles a UTC midnight mid-session.
  `run_from_execution()` returns `None` for anything without a shadow report, so
  a dry run or a live order is never priced as a shadow trade.
- **`eod.py` — the scorer.** Walks the contract's minute candles forward from the
  signal's arrival: stop hit → stopped at the stop; target reached → taken at the
  target; neither → marked to the close. **Deliberately pessimistic where the data
  is ambiguous**, which is where a backtest usually lies:
  - a bar whose range spans *both* stop and target counts as **stopped** (a minute
    candle records a range, not the order its extremes occurred in);
  - candles from before the signal arrived are ignored;
  - a contract whose data cannot be fetched is reported **unscored**, not dropped
    and not assumed flat, so the trade count always matches reality.
- **`FyersCandleSource`** — the broker's own `history` endpoint, so prices are what
  that contract actually traded at. One fetch per symbol per day (cached). A
  malformed bar is skipped rather than losing the day.
- **Index vs stock split (2026-09-28)** — the summary totals **index and stock
  options separately** (`is_index`, `DailyReport.index_trades` /
  `.stock_trades`, `subtotal()`). Prompted by the back-test below: on this
  channel they behave like two different strategies posted under one name, and a
  single combined figure hides which one is working. Each section carries its own
  P&L, win count and return on the capital that section used; a section with no
  trades is omitted.
- **Assumptions travel with the number** — the summary always states them
  (entry assumed filled at the signal's price; ambiguous bar = stopped; gross of
  costs). A P&L whose caveats are invisible is worse than no P&L, and these are
  asserted by a test so they cannot quietly disappear.
- **Wiring** — `Channel2Pipeline` takes an optional `ShadowRepository` and records
  each withheld order; a recording failure is caught and logged, never costing the
  user the alert they act on. `main.py` injects it.
- **`eod_report.py` + `deploy/teletrader-eod.{service,timer}`** — a one-shot unit
  fired at **15:35 IST, Mon-Fri** (`Persistent=true`, so a missed run still sends).
  `--print` renders without sending; `--date` re-scores an earlier day. With no
  FYERS token the report **still arrives**, with every trade unscored and saying so.
- Tests: `test_eod.py` (21) + pipeline recording tests. Suite **463 passing**.

**Known limits (stated, not hidden):** entry slippage is not modelled; brokerage,
STT and exchange charges are not deducted; and an `open` trade is marked to the
close rather than to a real square-off.

## Back-test of the stored channel-2 signals (2026-09-28)

Ran the 136 stored channel-2 signals against **real FYERS one-minute data** on
the droplet: 1 lot each, entered at the signal's stated price, exited at the
first target it names or its stop, whichever the contract reached first.

**67 of 136 could be priced.** The FYERS symbol master carries only *live*
contracts, so 65 signals from July to mid-August resolve to nothing (expired and
delisted) and 4 underlyings were unresolvable. Those are reported as skipped
rather than mispriced against a later expiry — resolving them to the current
month would have produced plausible, meaningless numbers. The priced window is
roughly **17 Aug – 25 Sep**.

| Cut | Trades | Win rate | Gross P&L |
| --- | ---: | ---: | ---: |
| All priced | 67 | 51% | **-15,777** |
| ...excluding 11 never filled | 56 | 61% | +180 |
| **NIFTY only** | 25 | **92%** | **+15,600** |
| **Stocks only** | 42 | 26% | **-31,377** |

- Average win **+874** vs average loss **-1,379**: break-even needs ~61%, and the
  stock signals deliver 26%.
- **Zero** trades were decided by an ambiguous bar, so the pessimistic tie-break
  did not shape the result.
- 11 signals named an entry the price never reached — no real fill would have
  happened. Excluding them the channel is flat, not profitable.
- Indicative costs (₹40 brokerage + 0.1% STT on the sell + 0.05% exchange + GST):
  all-priced nets ≈ **-21,600**; NIFTY-only nets ≈ **+13,900**.

**Read with care:** 25 NIFTY trades is a small sample and 92% will not persist;
entries are assumed filled exactly at the stated price (thin stock options would
slip, so the stock figure flatters); and only the *first* target is taken, per
the brief.

This is what shadow mode was built for — it produced the evidence before any
money was at risk. The end-of-day report was split index/stock as a direct
result, and the obvious follow-up is a **per-channel underlying allowlist** so a
channel can be limited to the instruments that actually work.

## Git state

- `.env`, `*.session`, `.venv/` are gitignored and NOT committed.
- Remote: `origin` → `git@github.com:axaxdhu/TeleTrader.git`. Branch `master`
  tracks `origin/master` and is in sync; working tree clean.
- Commits (newest first, as of 2026-09-27):
  - `b3a4458` Add Telegram bot notifications for recognised signals + outcomes
  - `e8bc219` Add FYERS live broker + per-channel broker selection
  - `65a8a63` Add second signal channel (parse-only) and trade-management commands
  - `9d9752e` Add live Zerodha Kite execution (Phase 5)
  - `528f29e` Update docs for the execution layer, dedupe, and pipeline wiring
  - `f62c2fa` Wire the pipeline end-to-end (parse -> store -> evaluate -> execute)
  - `c7b7801` Add broker-independent order execution layer (DryRunExecutor)
  - `64497a4` Make signal deduplication per trading day
  - `7d999cf` Size trades in lots via a LotSizeProvider seam
  - `63a708c` Add broker-agnostic trade engine (decision layer)
  - `687c464` Always offset target 2 points below, tolerate ++
  - `1703226` Phase 3 - SQLite persistence + open-ended target offset
  - `3681c08` Add compact Signal string formatting
  - `7b69803` Phase 2 - Signal parser
  - `1bd57f7` Support numeric channel IDs and add dialog-listing helper
  - `1dd4661` Phase 1 - Telegram listener

## Channel configuration

- `TELEGRAM_CHANNEL` accepts a `@username`, a numeric id, or `me` (Saved Messages).
- Numeric ids must be used **verbatim including the leading `-100…`**
  (Telethon "marked id"), e.g. `-1001163029526`.
- `config.py` has a `_parse_channel()` helper: all-numeric values are coerced to
  `int` (Telethon resolves numeric chats reliably only as ints); `@usernames`
  and `me` stay strings.
- **`list_dialogs.py`** (untracked throwaway) lists all your groups/channels with
  their ids: `uv run python list_dialogs.py`. Copy the id/username into `.env`.
- Current `.env` values (real channels, not `me`):
  - `TELEGRAM_CHANNEL=-1001163029526` (BULL CHIP PAID) — channel 1, full pipeline.
  - `TELEGRAM_CHANNEL_2=-1001524695283` (SafeTraders premium) — channel 2, parse-only.
  - `CHANNEL_1_ENABLED` / `CHANNEL_2_ENABLED` are unset, so both default to `true`.
  - Other channels the account is in: `@safetrader90` / `-1001387252520`, `@BULLCHIP`.

## Setup choices

- **uv** for env/deps (not pip/requirements.txt):
  - `uv venv --python 3.12`
  - `uv sync`
  - `uv run python main.py`
- Dependencies live in `pyproject.toml`; `uv.lock` is committed. No `requirements.txt`.
- **src layout**: `src/teletrader/` (`config.py`, `logging_config.py`, `telegram_listener.py`), entry point `main.py`.
- Config via env vars in `.env` (gitignored). Telegram API creds from <https://my.telegram.org>.
- `TELEGRAM_CHANNEL` now points at a real channel (see Channel configuration above); `me` (Saved Messages) remains the easiest value for local testing.
- `teletrader.session` (Telethon login session) exists and is gitignored, so runs no longer prompt for a login code.

## How to resume in a new chat

1. Point me at this file: "read docs/PROGRESS.md" (it is NOT auto-loaded).
2. `uv sync` if the venv is missing, then `uv run python main.py` to run.
3. `uv run pytest` to confirm the 322 tests pass.
4. Outstanding housekeeping: brokers need a fresh access token each day
   (`kite_login.py` / `fyers_login.py`; use the `--manual` paste flow on the
   cloud VM), and FYERS live orders require a whitelisted static IP.

## Next

*(reviewed 2026-09-28; suite 463 passing)*

All phases (1–5) are complete and **wired end-to-end**, plus trade-management
commands, a second (parse-only) channel, FYERS, and Telegram bot notifications.
A live signal runs parse→store→evaluate→execute. The broker is chosen
**per channel** — `CHANNEL_1_BROKER` / `CHANNEL_2_BROKER`, falling back to the
global `EXECUTION_MODE` — from three interchangeable executors: `dry_run`
(validate + log, sends nothing), `kite` (live Zerodha) and `fyers` (live FYERS).

**Current `.env` posture:** channel 1 = `-1001163029526` (BULL CHIP PAID, full
pipeline), channel 2 = `-1001524695283` (SafeTraders premium — now shadow-capable, set
`CHANNEL_2_BROKER=fyers_shadow` to arm it),
`EXECUTION_MODE=dry_run` with no per-channel override (so **nothing reaches a
real broker**), `AUTO_TRADING=true`, `MAX_TRADES_PER_DAY=3`, `TRADE_LOTS=1`,
`LOT_SIZES=NIFTY:65,BANKNIFTY:30`, hours 09:15–15:30 Asia/Kolkata,
`NOTIFY_ENABLED` unset (alerts off).

**To trade live:** set the channel's broker (`CHANNEL_1_BROKER=kite` or `fyers`,
or the global `EXECUTION_MODE`) and supply that broker's credentials —
`KITE_API_KEY` / `KITE_API_SECRET` / `KITE_ACCESS_TOKEN`, or `FYERS_APP_ID` /
`FYERS_SECRET_ID` / `FYERS_ACCESS_TOKEN`. Access tokens are **daily and manual**:
run `kite_login.py` / `fyers_login.py` (use `--manual` to paste the redirect URL
on the cloud VM). FYERS live orders additionally need a whitelisted static IP.
Also `AUTO_TRADING=true`, inside market hours. The engine gates everything before
the executor, so off-hours / auto-trading-off → `NOT_TRADED` and nothing reaches
the broker.

Live orders resolve to the exact broker symbol (nearest weekly expiry) via
`KiteInstrumentResolver` / `FyersInstrumentResolver` — no manual symbol mapping.

**Known follow-up (not started):**
- **Channel 2 to live FYERS** — see the shadow-mode section above for the four
  things that must land first (trade manager for ch2, trade-level source
  isolation, ch2 command parser, scale-out decision).
- **`BrokerLotSizeProvider` for channel 1 / Kite** — ✅ done for FYERS/channel 2;
  channel 1 still sizes from `LOT_SIZES`. `KiteInstrumentResolver` surfaces
  `lot_size`, so it is the same wiring.
- **Trade-management polish** (Phase 2 live Kite itself is ✅ done — SL-M + target
  LIMIT protection, bounded fill poll, app-managed OCO, live cancel/modify). Open
  items from those notes: reconcile cadence / postbacks instead of polling,
  `PENDING_ENTRY → OPEN` upgrade for late fills (they currently never receive
  protective orders), EOD square-off, configurable fill-poll window, real
  `MODIFY_TARGET` wording.
- **Pre-market connection timeout** seen on 2026-07-01 — unresolved; traceback not
  yet captured.
