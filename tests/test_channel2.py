"""Unit tests for the channel-2 signal parser.

Channel 2 has a looser, order-independent format covering *stock* options as well
as index options. These tests are built from real channel wording (see the
history captured while building the feature).
"""

from __future__ import annotations

import pytest

from teletrader.channel2 import looks_like_signal, parse_channel2_signal
from teletrader.parser import Action, OptionType


def test_parses_standard_signal() -> None:
    signal = parse_channel2_signal(
        "Nifty 23900 pe above 128\n\nLot 65\n\nTarget 140, 155, 175++\n\nSl 115\n\nCmp 124"
    )
    assert signal is not None
    assert signal.underlying == "NIFTY"
    assert signal.strike == 23900
    assert signal.option_type is OptionType.PUT
    assert signal.action is Action.BUY
    assert signal.entry_price == 128.0
    assert signal.stop_loss == 115.0


def test_uses_first_target_raw_no_offset() -> None:
    """Unlike channel 1, channel 2 keeps the FIRST target verbatim (no offset)."""
    signal = parse_channel2_signal(
        "Nifty 23900 pe above 128\n\nTarget 140, 155, 175++\n\nSl 115"
    )
    assert signal is not None
    assert signal.target == 140.0  # first of the list, not 138 (no -2 offset)
    assert signal.target_open_ended is True  # trailing ++ recorded


def test_target_without_plus_is_not_open_ended() -> None:
    signal = parse_channel2_signal(
        "Nifty 23900 ce above 159\n\nSl 145\n\nTarget 170, 190, 210"
    )
    assert signal is not None
    assert signal.target == 170.0
    assert signal.target_open_ended is False


def test_multi_word_underlying() -> None:
    signal = parse_channel2_signal(
        "Apollo hospital 8000 pe above 155\n\nLot 125\n\nTarget 165, 180, 200++\n\nSl 140"
    )
    assert signal is not None
    assert signal.underlying == "APOLLO HOSPITAL"
    assert signal.strike == 8000
    assert signal.option_type is OptionType.PUT


def test_field_order_is_irrelevant() -> None:
    """Sl/Target/Lot/Cmp may appear in any order; parsing is by label."""
    reordered = parse_channel2_signal(
        "Nifty 23500 ce above 165\n\nSl 155\n\nCmp 152\n\nTarget 175, 190++\n\nLot 65"
    )
    assert reordered is not None
    assert reordered.entry_price == 165.0
    assert reordered.stop_loss == 155.0
    assert reordered.target == 175.0


def test_decimals_are_preserved() -> None:
    signal = parse_channel2_signal(
        "Premierene 1080 ce above 48.25\n\nLot 575\n\nTarget 50.65, 52++\n\nSl 45"
    )
    assert signal is not None
    assert signal.entry_price == 48.25
    assert signal.target == 50.65
    assert signal.stop_loss == 45.0


def test_uppercase_target_label() -> None:
    signal = parse_channel2_signal(
        "Sonacom 645 ce above 26\n\nSl 24.50\n\nTARGET 27, 28.50, 30++"
    )
    assert signal is not None
    assert signal.target == 27.0


def test_stray_leading_dot_on_lot_is_tolerated() -> None:
    # ".lot 65" appears in real messages; Lot is ignored for sizing but must not
    # break parsing of the rest.
    signal = parse_channel2_signal(
        "Nifty 23500 ce above 165\n\n.lot 65\n\nSl 155\n\nTarget 175, 190++"
    )
    assert signal is not None
    assert signal.strike == 23500


def test_leading_chatter_line_is_ignored() -> None:
    signal = parse_channel2_signal(
        "One more\n\nNifty 23200 pe above 126\n\nLot 65\n\nTarget 136, 150++\n\nSl 116"
    )
    assert signal is not None
    assert signal.strike == 23200
    assert signal.entry_price == 126.0


def test_at_with_range_takes_first_number_as_entry() -> None:
    signal = parse_channel2_signal(
        "Nifty 23900 ce at  159-155\n\nSl 145\n\nTarget 170, 190, 210"
    )
    assert signal is not None
    assert signal.entry_price == 159.0


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "   ",
        "173🔥🔥🔥\n\nSafe target Done ✔️\n\n15++ points",  # target-hit narration
        "sl hit on this",
        "Type mistake",
        "No entries till now, looking for a good entry. Just wait",
        "https://youtu.be/sROZDZpYovM",
    ],
)
def test_noise_is_rejected(text: str | None) -> None:
    assert parse_channel2_signal(text) is None


def test_missing_stop_loss_is_rejected() -> None:
    # Header + target but no numeric Sl — a miss is preferred to a bad parse.
    assert parse_channel2_signal("Nifty 23900 pe above 128\n\nTarget 140, 155++") is None


def test_missing_target_is_rejected() -> None:
    assert parse_channel2_signal("Nifty 23900 pe above 128\n\nSl 115") is None


# --- Missed-signal detection --------------------------------------------------
#
# The parser rejects anything it cannot read exactly, which is right, but on a
# loosely formatted channel that silence can hide a real trade. These tests pin
# the line between "worth a look" and "ordinary chatter".


@pytest.mark.parametrize(
    "message",
    [
        "Coforge 1500 ce above 70\nsl-- sixty six",          # stop unreadable
        "Apollo hospital 7500 ce above 307 sl 289",          # all on one line
        "Buy 23900 PE",                                      # contract, no fields
        "Target 140, 155",                                   # a stray entry field
        "Sl 115",
    ],
)
def test_signal_like_misses_are_flagged(message: str) -> None:
    assert parse_channel2_signal(message) is None
    assert looks_like_signal(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "🔥 Coforge 1500 ce Safe target Done ✔️",  # outcome narration
        "sl hit on this",
        "1500 ce booked profit",
        "Type mistake",
        "ignore the last one",
        "Cmp 124",
        "One more",
        "good morning traders",
        "",
        None,
    ],
)
def test_chatter_is_not_flagged(message: str | None) -> None:
    assert looks_like_signal(message) is False
