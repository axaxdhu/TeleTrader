# Telegram Trader

Listens to Telegram trading signals and (in later phases) places trades through a
broker API. Implemented so far: connect to Telegram and listen for new messages
(Phase 1), deterministically parse them into structured signals (Phase 2),
persist them to SQLite with per-day duplicate rejection (Phase 3), run each signal
through a broker-agnostic **trade engine** that decides whether it should be
traded, and hand accepted orders to an **execution layer** (Phase 4). A live
message now runs the full path *parse → evaluate → store → execute* via the
`SignalPipeline`.

The trade engine is the decision layer only — it never talks to a broker and
never places orders. Accepted orders go to a broker-independent **execution
layer**: an `Executor` chosen by broker mode. `DryRunExecutor` (`dry_run`)
validates an order, logs exactly what *would* be submitted, and records the
attempt to SQLite — without contacting any broker. Two live executors implement
the same interface: `KiteExecutor` (`kite`, Zerodha) and `FyersExecutor`
(`fyers`), each resolving the exact broker symbol/expiry from that broker's
instrument master and managing protective SL-M + target orders with app-side OCO.

The broker is selectable **per channel** (`CHANNEL_1_BROKER` / `CHANNEL_2_BROKER`,
falling back to the global `EXECUTION_MODE`), so different channels can trade
through different brokers — switching a channel's broker is a one-line config
change and nothing upstream (engine, pipeline, listener) is aware of it.

## Project structure

```
TeleTrader/
├── main.py                      # Entry point
├── pyproject.toml               # Package metadata + dependencies (src layout)
├── uv.lock                      # Pinned, resolved dependency lockfile
├── .env.example                 # Template for required environment variables
├── .gitignore
└── src/
    └── teletrader/
        ├── __init__.py
        ├── config.py            # Env-var configuration (dataclass)
        ├── logging_config.py    # Structured logging setup
        ├── parser.py            # Deterministic regex signal parser
        ├── database.py          # SQLite connection + migrations
        ├── repository.py        # Signal persistence + dedupe
        ├── lot_size.py          # LotSizeProvider (config now, broker later)
        ├── trade_engine.py      # Business-logic decision layer (no broker)
        ├── execution/           # Order execution layer (DryRun / Kite / FYERS)
        └── telegram_listener.py # Telethon listener
```

## Requirements

- Python 3.12+
- Telegram API credentials from <https://my.telegram.org/apps>

## Environment variables

Copy `.env.example` to `.env` and fill in the values:

| Variable                 | Required | Description                                              |
| ------------------------ | -------- | -------------------------------------------------------- |
| `TELEGRAM_API_ID`        | yes      | Numeric API ID from my.telegram.org                      |
| `TELEGRAM_API_HASH`      | yes      | API hash from my.telegram.org                            |
| `TELEGRAM_PHONE`         | yes      | Login phone in international format (e.g. `+9198...`)     |
| `TELEGRAM_CHANNEL`       | yes      | Channel to listen to (`@username` or numeric chat id)    |
| `TELEGRAM_SESSION_NAME`  | no       | Session file name (default `teletrader`)                 |
| `LOG_LEVEL`              | no       | `DEBUG`/`INFO`/`WARNING`/`ERROR` (default `INFO`)         |
| `DATABASE_PATH`          | no       | SQLite file for stored signals (default `teletrader.db`) |
| `AUTO_TRADING`           | no       | Master go/no-go for the trade engine (default `false`)   |
| `ALLOW_DUPLICATES`       | no       | Let the engine act on repeated signals (default `false`) |
| `MAX_TRADES_PER_DAY`     | no       | Daily cap the engine enforces (default `10`)             |
| `TRADE_LOTS`             | no       | Lots per accepted signal; quantity = lots × lot size (default `1`) |
| `LOT_SIZES`              | no       | Per-underlying lot size, e.g. `NIFTY:65,BANKNIFTY:30` (default empty → unsized underlyings rejected) |
| `MARKET_OPEN_TIME`       | no       | Trading-hours start, `HH:MM` (default `09:15`)           |
| `MARKET_CLOSE_TIME`      | no       | Trading-hours end, `HH:MM` (default `15:30`)             |
| `MARKET_TIMEZONE`        | no       | IANA tz for trading hours (default `Asia/Kolkata`)       |
| `EXECUTION_MODE`         | no       | Global default broker: `dry_run` (default), `kite`, or `fyers` |
| `CHANNEL_1_BROKER`       | no       | Per-channel broker override for channel 1 (falls back to `EXECUTION_MODE`) |
| `CHANNEL_2_BROKER`       | no       | Per-channel broker override for channel 2 (falls back to `EXECUTION_MODE`) |
| `KITE_API_KEY` / `KITE_API_SECRET` / `KITE_ACCESS_TOKEN` | if `kite` | Zerodha Kite creds; token is manual/daily (`kite_login.py`) |
| `FYERS_APP_ID` / `FYERS_SECRET_ID` / `FYERS_ACCESS_TOKEN` | if `fyers` | FYERS creds; token is manual/daily (`fyers_login.py`). Live orders need a whitelisted static IP |
| `NOTIFY_ENABLED`         | no       | Send push alerts (recognised signal + outcome) via a Telegram bot (default `false`) |
| `NOTIFY_BOT_TOKEN` / `NOTIFY_CHAT_ID` | if notify | Bot token from @BotFather + your chat id (secret; needed when `NOTIFY_ENABLED=true`) |

## Installation

This project uses [uv](https://docs.astral.sh/uv/).

```bash
# 1. Create the virtual environment (uv fetches Python 3.12 if needed)
uv venv --python 3.12

# 2. Install the project and its locked dependencies
uv sync

# 3. Configure credentials
cp .env.example .env
# edit .env with your values
```

## Running locally

```bash
uv run python main.py
```

(or `source .venv/bin/activate` first, then `python main.py`)

On first run Telethon prompts for the login code sent to your Telegram account
(and your 2FA password if enabled). This creates a `<session_name>.session` file
so subsequent runs connect without prompting.

Incoming messages run through the pipeline; each prints a one-line outcome and is
logged. A valid signal that the engine accepts produces a dry-run execution, e.g.:

```
[10:00:00] SIGNAL #7 BUY NIFTY 23900 PE @165 SL 150 TGT 198+
           → SUCCESS: Order would have been submitted successfully.
```

with the full `[DRY RUN]` order block in the logs. Non-signals print `(ignored)`,
a same-day repeat prints `(duplicate)`, and a signal the engine declines prints
`→ not traded: <reason>` (e.g. `Auto-trading disabled` when `AUTO_TRADING=false`,
the default). Stop with `Ctrl+C`.
