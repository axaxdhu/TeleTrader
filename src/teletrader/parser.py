"""Deterministic signal parser.

Parses Telegram option-trading signals of the fixed form::

    NIFTY 23900 PE ABOVE 165

    SL-150

    TGT-200+

into a structured :class:`Signal`. Parsing is pure regex — no AI/LLM. Anything
that does not match the exact three-line shape is treated as noise and rejected
(``parse_signal`` returns ``None``).

Scope: index options (NIFTY / BANKNIFTY) only. Equities are out of scope for now
but the ``Signal`` shape is intentionally small so a future instrument variant
can be added without disturbing callers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from .logging_config import get_logger

__all__ = ["Action", "OptionType", "Signal", "parse_signal"]

logger = get_logger(__name__)

# Keep log lines from blowing up on accidental walls of text.
_MAX_LOGGED_CHARS = 120

# When a target carries a trailing ``+`` (open-ended, "200 and beyond"), we do
# not aim for the exact level — the stored target is set this many points
# *below* it so the exit fills before price stalls at the round number.
TARGET_PLUS_OFFSET = 2.0


class OptionType(str, Enum):
    """Option right."""

    CALL = "CE"
    PUT = "PE"


class Action(str, Enum):
    """Trade direction."""

    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True, slots=True)
class Signal:
    """A parsed, structured trading signal (index option).

    ``action`` is always :attr:`Action.BUY`: an ``ABOVE`` entry is a breakout
    that is entered by *buying* the option. ``target_open_ended`` records that
    the source message carried a trailing ``+`` on the target (e.g. ``TGT-200+``,
    meaning "200 and beyond"). When it did, ``target`` is **not** the raw level
    from the message: it is set :data:`TARGET_PLUS_OFFSET` points *below* it
    (e.g. ``TGT-200+`` -> ``target == 198``) so the exit fills before price
    stalls at the round number. ``target_open_ended`` is kept purely for audit —
    it lets you see the original had a ``+`` after the adjustment is applied.
    """

    underlying: str
    strike: int
    option_type: OptionType
    action: Action
    entry_price: float
    stop_loss: float
    target: float
    target_open_ended: bool
    raw_text: str

    def __str__(self) -> str:
        """Compact, human-readable one-liner for console/log output.

        e.g. ``BUY NIFTY 23900 PE @165 SL 150 TGT 200+``.
        """
        plus = "+" if self.target_open_ended else ""
        return (
            f"{self.action.value} {self.underlying} {self.strike} "
            f"{self.option_type.value} @{_fmt(self.entry_price)} "
            f"SL {_fmt(self.stop_loss)} TGT {_fmt(self.target)}{plus}"
        )


def _fmt(value: float) -> str:
    """Render a price without a trailing ``.0`` (165.0 -> "165", 165.5 -> "165.5")."""
    return str(int(value)) if value.is_integer() else str(value)


# --- Regexes for the three content lines --------------------------------------
# A bare number with an optional decimal part.
_NUM = r"\d+(?:\.\d+)?"

_HEADER_RE = re.compile(
    rf"^(?P<underlying>BANKNIFTY|NIFTY)\s+"
    rf"(?P<strike>\d+)\s+"
    rf"(?P<option_type>CE|PE)\s+"
    rf"ABOVE\s+"
    rf"(?P<entry>{_NUM})$",
    re.IGNORECASE,
)
_SL_RE = re.compile(rf"^SL\s*-\s*(?P<sl>{_NUM})$", re.IGNORECASE)
_TGT_RE = re.compile(rf"^TGT\s*-\s*(?P<tgt>{_NUM})\s*(?P<open>\+?)$", re.IGNORECASE)


def parse_signal(message: str | None) -> Signal | None:
    """Parse ``message`` into a :class:`Signal`, or return ``None`` if it is not
    a valid signal.

    Rejection is graceful: non-matching messages (channel chatter, malformed
    signals, empty/None) yield ``None`` and a logged warning — never an
    exception.
    """
    if not message or not message.strip():
        return None

    lines = [line.strip() for line in message.strip().splitlines()]
    lines = [line for line in lines if line]
    if len(lines) != 3:
        return _reject(message, f"expected 3 content lines, got {len(lines)}")

    header_match = _HEADER_RE.match(lines[0])
    sl_match = _SL_RE.match(lines[1])
    tgt_match = _TGT_RE.match(lines[2])
    if not (header_match and sl_match and tgt_match):
        return _reject(message, "does not match signal format")

    # An open-ended (``+``) target is pulled in by TARGET_PLUS_OFFSET points;
    # an explicit target is taken verbatim. See TARGET_PLUS_OFFSET above.
    open_ended = bool(tgt_match.group("open"))
    raw_target = float(tgt_match.group("tgt"))
    target = raw_target - TARGET_PLUS_OFFSET if open_ended else raw_target

    signal = Signal(
        underlying=header_match.group("underlying").upper(),
        strike=int(header_match.group("strike")),
        option_type=OptionType(header_match.group("option_type").upper()),
        action=Action.BUY,
        entry_price=float(header_match.group("entry")),
        stop_loss=float(sl_match.group("sl")),
        target=target,
        target_open_ended=open_ended,
        raw_text=message,
    )
    logger.info("Parsed signal: %s", signal)
    return signal


def _reject(message: str, reason: str) -> None:
    """Log a rejected message at DEBUG and return ``None``.

    Rejections are logged at DEBUG (not WARNING) because, on a live channel, most
    messages are ordinary chatter rather than malformed signals — warning on each
    would flood the logs.
    """
    snippet = message.strip().replace("\n", " ⏎ ")
    if len(snippet) > _MAX_LOGGED_CHARS:
        snippet = snippet[:_MAX_LOGGED_CHARS] + "…"
    logger.debug("Rejected message (%s): %r", reason, snippet)
    return None
