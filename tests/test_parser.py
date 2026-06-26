"""Unit tests for the deterministic signal parser."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from teletrader.parser import (
    TARGET_PLUS_OFFSET,
    Action,
    OptionType,
    Signal,
    parse_signal,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sample_messages.md"


def _load_fixture_signals() -> list[str]:
    """Extract every ``>>> ... <<<`` block from the sample fixture file."""
    text = FIXTURE.read_text(encoding="utf-8")
    return [block.strip() for block in re.findall(r">>>\n(.*?)\n<<<", text, re.DOTALL)]


# --- Valid signals ------------------------------------------------------------

@pytest.mark.parametrize("raw", _load_fixture_signals())
def test_every_fixture_sample_parses(raw: str) -> None:
    """Each real sample message must parse into a Signal."""
    signal = parse_signal(raw)
    assert signal is not None
    assert signal.underlying in {"NIFTY", "BANKNIFTY"}
    assert signal.action is Action.BUY


def test_parses_all_fields_correctly() -> None:
    signal = parse_signal("NIFTY 23900 PE ABOVE 165\n\nSL-150\n\nTGT-200+")
    assert signal == Signal(
        underlying="NIFTY",
        strike=23900,
        option_type=OptionType.PUT,
        action=Action.BUY,
        entry_price=165.0,
        stop_loss=150.0,
        target=198.0,  # 200+ pulled in by TARGET_PLUS_OFFSET (2)
        target_open_ended=True,
        raw_text="NIFTY 23900 PE ABOVE 165\n\nSL-150\n\nTGT-200+",
    )


def test_open_ended_target_is_offset_below_signal_value() -> None:
    """A trailing ``+`` stores the target TARGET_PLUS_OFFSET points below."""
    signal = parse_signal("BANKNIFTY 51000 CE ABOVE 240\n\nSL-200\n\nTGT-320+")
    assert signal is not None
    assert signal.target == 320.0 - TARGET_PLUS_OFFSET
    assert signal.target_open_ended is True  # flag retained for audit


def test_explicit_target_is_not_offset() -> None:
    """No ``+`` -> target is taken verbatim, no offset applied."""
    signal = parse_signal("NIFTY 24250 PE ABOVE 130\n\nSL-115\n\nTGT-170")
    assert signal is not None
    assert signal.target == 170.0
    assert signal.target_open_ended is False


def test_call_option_and_banknifty() -> None:
    signal = parse_signal("BANKNIFTY 56600 CE  ABOVE 260\n\nSL-220\n\nTGT-340+")
    assert signal is not None
    assert signal.underlying == "BANKNIFTY"
    assert signal.option_type is OptionType.CALL
    assert signal.strike == 56600
    assert signal.entry_price == 260.0


def test_target_without_plus_is_not_open_ended() -> None:
    signal = parse_signal("NIFTY 24250 PE ABOVE 130\n\nSL-115\n\nTGT-170")
    assert signal is not None
    assert signal.target == 170.0
    assert signal.target_open_ended is False


def test_double_spaces_are_tolerated() -> None:
    assert parse_signal("BANKNIFTY 51500 PE  ABOVE 380\n\nSL-340\n\nTGT-450+") is not None


def test_lowercase_is_tolerated() -> None:
    signal = parse_signal("nifty 23900 ce above 130\n\nsl-115\n\ntgt-170+")
    assert signal is not None
    assert signal.underlying == "NIFTY"
    assert signal.option_type is OptionType.CALL


def test_decimal_prices() -> None:
    signal = parse_signal("NIFTY 23900 PE ABOVE 165.5\n\nSL-150.25\n\nTGT-200.75+")
    assert signal is not None
    assert signal.entry_price == 165.5
    assert signal.stop_loss == 150.25
    assert signal.target == 200.75 - TARGET_PLUS_OFFSET  # 198.75


def test_surrounding_blank_lines_are_ignored() -> None:
    assert parse_signal("\n\nNIFTY 23900 PE ABOVE 165\n\nSL-150\n\nTGT-200+\n\n") is not None


# --- Noise / invalid input (must reject gracefully -> None) -------------------

@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "Good morning traders! Market looks bullish today 🚀",
        "Booked profit in NIFTY 23900 PE, congrats all 🎉",
        "NIFTY 23900 PE ABOVE 165",  # missing SL and TGT lines
        "NIFTY 23900 PE ABOVE 165\n\nSL-150",  # missing TGT line
        "NIFTY 23900 XX ABOVE 165\n\nSL-150\n\nTGT-200+",  # bad option type
        "SENSEX 23900 PE ABOVE 165\n\nSL-150\n\nTGT-200+",  # unsupported underlying
        "NIFTY 23900 PE BELOW 165\n\nSL-150\n\nTGT-200+",  # wrong trigger word
        "NIFTY 23900 PE ABOVE 165\n\nSTOP-150\n\nTGT-200+",  # malformed SL
        "NIFTY PE ABOVE 165\n\nSL-150\n\nTGT-200+",  # missing strike
        "NIFTY 23900 PE ABOVE 165\n\nSL-150\n\nTGT-200+\n\nextra line",  # 4 lines
    ],
)
def test_rejects_noise_and_malformed(raw: str) -> None:
    assert parse_signal(raw) is None


def test_none_input_is_rejected() -> None:
    assert parse_signal(None) is None


# --- Compact string formatting -----------------------------------------------

def test_str_is_compact_and_drops_trailing_zero() -> None:
    # 200+ is stored as 198 (offset); the trailing + still marks it open-ended.
    signal = parse_signal("NIFTY 23900 PE ABOVE 165\n\nSL-150\n\nTGT-200+")
    assert str(signal) == "BUY NIFTY 23900 PE @165 SL 150 TGT 198+"


def test_str_without_open_ended_target_has_no_plus() -> None:
    signal = parse_signal("NIFTY 23900 PE ABOVE 165\n\nSL-150\n\nTGT-200")
    assert str(signal) == "BUY NIFTY 23900 PE @165 SL 150 TGT 200"


def test_str_keeps_decimals() -> None:
    signal = parse_signal("BANKNIFTY 56600 CE ABOVE 260.5\n\nSL-220\n\nTGT-340.25+")
    assert str(signal) == "BUY BANKNIFTY 56600 CE @260.5 SL 220 TGT 338.25+"
