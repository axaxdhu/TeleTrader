"""Unit tests for the deterministic trade-management command parser."""

from __future__ import annotations

import pytest

from teletrader.commands import (
    CommandAction,
    ManagementCommand,
    PriceRef,
    parse_command,
    parse_message,
)
from teletrader.parser import Signal

# A canonical valid entry signal (three-line shape) for the dispatch tests.
_SIGNAL = "NIFTY 23900 PE ABOVE 165\n\nSL-150\n\nTGT-200+"


# --- Avoid --------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw",
    [
        "Avoid",
        "AVOID",
        "avoid",
        "  Avoid  ",
        "Avoid!",
        "Avoid.",
        "🚫 Avoid",
        "Avoid trade",
        "Avoid this trade",
        "AVOID THE TRADE",
        "avoid our trade",
    ],
)
def test_avoid_variants(raw: str) -> None:
    command = parse_command(raw)
    assert command is not None
    assert command.action is CommandAction.AVOID
    assert command.to is None and command.price is None
    assert command.raw_text == raw


# --- Book profit --------------------------------------------------------------

@pytest.mark.parametrize(
    "raw",
    [
        "SAFE TRADERS BOOK PROFIT",
        "Book Profit",
        "book profit",
        "BOOK PROFITS",
        "Book full profit",
        "✅ BOOK PROFIT ✅",
        "  SAFE TRADERS   BOOK PROFIT  ",
        "SAFE TRADERS\nBOOK PROFIT",
    ],
)
def test_book_profit_variants(raw: str) -> None:
    command = parse_command(raw)
    assert command is not None
    assert command.action is CommandAction.BOOK_PROFIT
    assert command.to is None and command.price is None


# --- Modify stop-loss ---------------------------------------------------------

@pytest.mark.parametrize(
    "raw",
    [
        "MODIFY SL TO COST",
        "Modify SL to cost",
        "SL TO COST",
        "move sl to entry",
        "MODIFY SL TO ENTRY PRICE",
        "Trail SL to cost",
        "🟢 MODIFY SL TO COST 🟢",
    ],
)
def test_modify_sl_to_cost(raw: str) -> None:
    command = parse_command(raw)
    assert command is not None
    assert command.action is CommandAction.MODIFY_STOP_LOSS
    assert command.to is PriceRef.COST
    assert command.price is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("SL TO 150", 150.0),
        ("MODIFY SL TO 150.5", 150.5),
        ("move sl to 200", 200.0),
    ],
)
def test_modify_sl_to_explicit_price(raw: str, expected: float) -> None:
    command = parse_command(raw)
    assert command is not None
    assert command.action is CommandAction.MODIFY_STOP_LOSS
    assert command.price == expected
    assert command.to is None


# --- Modify target (tentative wording) ----------------------------------------

def test_modify_target_to_price() -> None:
    command = parse_command("MOVE TARGET TO 250")
    assert command is not None
    assert command.action is CommandAction.MODIFY_TARGET
    assert command.price == 250.0


# --- Negative cases: chatter and signals must NOT look like commands ----------

@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "you can book profit later if you want",
        "should we avoid the market today guys?",
        "great trade everyone",
        "SL-150",                       # a signal fragment, not a command
        _SIGNAL,                        # a full entry signal is not a command
        "modify your stoploss carefully",
    ],
)
def test_non_commands_return_none(raw: str) -> None:
    assert parse_command(raw) is None


def test_parse_command_ignores_none() -> None:
    assert parse_command(None) is None


# --- Dispatch via parse_message ----------------------------------------------

def test_parse_message_returns_signal_for_entry() -> None:
    result = parse_message(_SIGNAL)
    assert isinstance(result, Signal)
    assert result.underlying == "NIFTY"


def test_parse_message_returns_command_for_management_message() -> None:
    result = parse_message("MODIFY SL TO COST")
    assert isinstance(result, ManagementCommand)
    assert result.action is CommandAction.MODIFY_STOP_LOSS


def test_parse_message_returns_none_for_noise() -> None:
    assert parse_message("good morning traders") is None


def test_signal_is_not_misread_as_command() -> None:
    """An entry signal must dispatch to the Signal path, never a command."""
    assert not isinstance(parse_message(_SIGNAL), ManagementCommand)


# --- Rendering ----------------------------------------------------------------

def test_command_str_is_readable() -> None:
    assert str(ManagementCommand(CommandAction.AVOID, "Avoid")) == "AVOID"
    assert (
        str(ManagementCommand(CommandAction.BOOK_PROFIT, "x")) == "BOOK PROFIT"
    )
    assert (
        str(ManagementCommand(CommandAction.MODIFY_STOP_LOSS, "x", to=PriceRef.COST))
        == "MODIFY SL TO cost"
    )
    assert (
        str(ManagementCommand(CommandAction.MODIFY_TARGET, "x", price=250.0))
        == "MODIFY TGT TO 250"
    )
