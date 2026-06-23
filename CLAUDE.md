# Telegram Trader

## Project Goal

Build a Python application that listens to Telegram trading signals and automatically places trades through a broker API.

Current broker target:

* Zerodha Kite Connect

Future broker targets:

* FYERS

The system must be broker-agnostic.

## Architecture

Telegram Listener
→ Signal Parser
→ Risk Manager
→ Broker Adapter
→ Trade Database
→ Notifications

## Principles

* Keep the system simple.
* Avoid overengineering.
* Prefer SQLite over external databases.
* Use environment variables for secrets.
* No web UI for initial versions.
* No AI/LLM parsing.
* Signals will have a fixed format.
* Use regex and deterministic parsing.

## Development Phases

Phase 1:
Telegram listener only.

Phase 2:
Signal parser.

Phase 3:
Database storage.

Phase 4:
Paper trading.

Phase 5:
Live broker integration.

## Coding Standards

* Python 3.12+
* Type hints everywhere.
* Dataclasses preferred.
* Structured logging.
* Modular architecture.
* Dependency injection where appropriate.

## Initial Scope

The first milestone is ONLY:

* Connect to Telegram
* Listen for new messages
* Print received messages
* No parsing
* No trading
* No database

Do not implement future phases unless explicitly requested.
