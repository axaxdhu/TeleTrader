# Telegram Trader

Listens to Telegram trading signals and (in later phases) places trades through a
broker API. Implemented so far: connect to Telegram and listen for new messages
(Phase 1), deterministically parse them into structured signals (Phase 2),
persist them to SQLite with duplicate rejection (Phase 3), and run each signal
through a broker-agnostic **trade engine** that decides whether it should be
traded.

The trade engine is the business-logic layer only — it never talks to a broker
and never places orders. Broker integration (paper trading, then live Zerodha /
FYERS) lands in Phases 4–5.

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
        ├── trade_engine.py      # Business-logic decision layer (no broker)
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
| `TRADE_QUANTITY`         | no       | Quantity per accepted signal (default `15`)              |
| `MARKET_OPEN_TIME`       | no       | Trading-hours start, `HH:MM` (default `09:15`)           |
| `MARKET_CLOSE_TIME`      | no       | Trading-hours end, `HH:MM` (default `15:30`)             |
| `MARKET_TIMEZONE`        | no       | IANA tz for trading hours (default `Asia/Kolkata`)       |

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

Incoming messages are printed to the console and logged, e.g.:

```
2026-06-24 10:00:00 | INFO     | teletrader.telegram_listener | Received message id=42 chat_id=-1001234567890: BUY NIFTY 25000 CE
[2026-06-24 10:00:00] BUY NIFTY 25000 CE
```

Stop with `Ctrl+C`.
