"""Deterministic parser for trade-management commands.

Beyond the entry signals handled by :mod:`teletrader.parser`, a signal channel
also posts short *management* messages that act on an already-placed trade rather
than open a new one, e.g.::

    Avoid
    SAFE TRADERS BOOK PROFIT
    MODIFY SL TO COST

These are parsed here into a :class:`ManagementCommand`. Parsing is pure regex —
no AI/LLM — and deliberately **strict**: the patterns are anchored, near-exact
matches against the whole (normalised) message. A management command can cancel
or exit a *real* position, so a false positive is worse than a miss; loosening a
pattern should be driven by observed channel wording, not guesswork.

:func:`parse_message` is the single entry point for an inbound message: it returns
a :class:`~teletrader.parser.Signal` (an entry signal), a
:class:`ManagementCommand` (a management command), or ``None`` (noise).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from .logging_config import get_logger
from .parser import Signal, parse_signal

__all__ = [
    "CommandAction",
    "ManagementCommand",
    "PriceRef",
    "parse_command",
    "parse_message",
]

logger = get_logger(__name__)


class CommandAction(str, Enum):
    """A management action on an existing trade.

    The parser only identifies *what* the channel asked for; *which* trade it
    applies to, and *how* to carry it out on the broker, are decided downstream.
    """

    AVOID = "AVOID"                       # cancel the entry if placed but not yet filled
    BOOK_PROFIT = "BOOK_PROFIT"           # exit the open position now, at market
    MODIFY_STOP_LOSS = "MODIFY_STOP_LOSS"  # move the protective stop-loss
    MODIFY_TARGET = "MODIFY_TARGET"        # move the target / take-profit


class PriceRef(str, Enum):
    """A *symbolic* price a stop-loss/target can be moved to (vs an absolute number)."""

    COST = "COST"   # the trade's entry price — i.e. move the stop to breakeven


@dataclass(frozen=True, slots=True)
class ManagementCommand:
    """A parsed management command targeting an existing trade.

    ``action`` is the only required field. For a stop-loss/target move, the new
    level is carried either symbolically (``to=PriceRef.COST`` for "to cost" —
    move to the entry price) or as an explicit number (``price``); exactly one of
    the two is set, and both are ``None`` for ``AVOID``/``BOOK_PROFIT``.
    """

    action: CommandAction
    raw_text: str
    to: PriceRef | None = None
    price: float | None = None

    def __str__(self) -> str:
        if self.action in (CommandAction.MODIFY_STOP_LOSS, CommandAction.MODIFY_TARGET):
            where = self.to.value.lower() if self.to is not None else _fmt(self.price)
            what = "SL" if self.action is CommandAction.MODIFY_STOP_LOSS else "TGT"
            return f"MODIFY {what} TO {where}"
        return self.action.value.replace("_", " ")


def _fmt(value: float | None) -> str:
    """Render a price without a trailing ``.0`` (150.0 -> "150", 150.5 -> "150.5")."""
    if value is None:
        return "?"
    return str(int(value)) if value.is_integer() else str(value)


# --- Patterns -----------------------------------------------------------------
# A bare number with an optional decimal part (shared with the signal parser).
_NUM = r"\d+(?:\.\d+)?"

# All patterns are anchored (``^…$``) and matched against the *normalised core*
# of the message (see ``_normalise``): surrounding whitespace, edge punctuation,
# and emoji are stripped, and internal runs of whitespace are collapsed to single
# spaces. Strictness is intentional — see the module docstring.

_AVOID_RE = re.compile(
    r"^AVOID(?:\s+(?:THIS\s+|THE\s+|OUR\s+)?TRADE)?$",
    re.IGNORECASE,
)

# Tolerates a leading channel-branding prefix (e.g. "SAFE TRADERS ") and an
# optional "FULL"/plural. Add further branding prefixes here as they are observed.
_BOOK_PROFIT_RE = re.compile(
    r"^(?:SAFE\s+TRADERS\s+)?BOOK\s+(?:FULL\s+)?PROFITS?$",
    re.IGNORECASE,
)

# "MODIFY SL TO COST" and variants. The leading verb is optional ("SL TO COST"),
# and the destination is either symbolic ("COST" / "ENTRY" / "ENTRY PRICE") or an
# explicit number ("SL TO 150").
_MODIFY_SL_RE = re.compile(
    r"^(?:(?:MODIFY|MOVE|TRAIL|SHIFT|CHANGE)\s+)?SL\s+TO\s+"
    rf"(?:(?P<cost>COST|ENTRY(?:\s+PRICE)?)|(?P<price>{_NUM}))$",
    re.IGNORECASE,
)

# TENTATIVE — no real "move target" message has been provided yet. Mirrors the SL
# pattern; confirm/adjust against actual channel wording before relying on it.
_MODIFY_TGT_RE = re.compile(
    r"^(?:(?:MODIFY|MOVE|TRAIL|SHIFT|CHANGE)\s+)?(?:TGT|TARGET)\s+TO\s+"
    rf"(?:(?P<cost>COST|ENTRY(?:\s+PRICE)?)|(?P<price>{_NUM}))$",
    re.IGNORECASE,
)


def parse_message(message: str | None) -> Signal | ManagementCommand | None:
    """Parse one inbound message into a signal, a command, or ``None`` (noise).

    Entry signals are tried first (their fixed three-line shape is unambiguous);
    anything else is offered to the command parser. This is the single entry point
    callers should use.
    """
    signal = parse_signal(message)
    if signal is not None:
        return signal
    return parse_command(message)


def parse_command(message: str | None) -> ManagementCommand | None:
    """Parse ``message`` into a :class:`ManagementCommand`, or ``None`` if it is
    not a recognised command.

    Like the signal parser, rejection is graceful — ordinary chatter yields
    ``None`` (logged at DEBUG), never an exception.
    """
    if not message or not message.strip():
        return None

    core = _normalise(message)
    if not core:
        return None

    if _AVOID_RE.match(core):
        return _parsed(ManagementCommand(CommandAction.AVOID, message))
    if _BOOK_PROFIT_RE.match(core):
        return _parsed(ManagementCommand(CommandAction.BOOK_PROFIT, message))

    match = _MODIFY_SL_RE.match(core)
    if match:
        return _parsed(_move(CommandAction.MODIFY_STOP_LOSS, message, match))

    match = _MODIFY_TGT_RE.match(core)
    if match:
        return _parsed(_move(CommandAction.MODIFY_TARGET, message, match))

    return None


def _normalise(message: str) -> str:
    """Collapse whitespace and strip edge punctuation/emoji to a matchable core.

    e.g. ``"  ✅ BOOK PROFIT ✅ "`` -> ``"BOOK PROFIT"`` and
    ``"SAFE TRADERS\\nBOOK PROFIT"`` -> ``"SAFE TRADERS BOOK PROFIT"``.
    """
    collapsed = re.sub(r"\s+", " ", message).strip()
    return re.sub(r"^[^\w]+|[^\w]+$", "", collapsed)


def _move(
    action: CommandAction, message: str, match: re.Match[str]
) -> ManagementCommand:
    """Build a stop-loss/target move command from a regex match."""
    if match.group("cost") is not None:
        return ManagementCommand(action, message, to=PriceRef.COST)
    return ManagementCommand(action, message, price=float(match.group("price")))


def _parsed(command: ManagementCommand) -> ManagementCommand:
    logger.info("Parsed command: %s", command)
    return command
