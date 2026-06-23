# Telegram Trader

Listens to Telegram trading signals and (in later phases) places trades through a
broker API. This milestone implements **Phase 1 only**: connect to Telegram,
listen for new messages from a configured channel, and print + log them.

No parsing, no broker integration, no database.

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
