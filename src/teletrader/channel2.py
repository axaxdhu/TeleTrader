"""Deterministic parser for the *second* signal channel.

Channel 2 posts option-trading signals with the same *intent* as the primary
channel (:mod:`teletrader.parser`) but a very different, looser layout::

    Nifty 23900 pe above 128

    Lot 65

    Target 140, 155, 175++

    Sl 115

    Cmp 124

Key differences from channel 1 (which drive this separate parser):

* **Variable field order** — ``Lot`` / ``Target`` / ``Sl`` / ``Cmp`` appear in
  any order, so fields are matched **by label**, not by line position.
* **Any underlying** — mostly *stock* options (e.g. ``Apollo hospital``,
  ``Bharatforge``), not just NIFTY/BANKNIFTY. Underlyings can be multi-word.
* **Multiple targets** — a comma-separated list; per project decision only the
  **first** ("safe") target is used, and it is taken **raw** (no offset — unlike
  channel 1, whose small stock-option premiums make a fixed points offset wrong).
* Free-form chatter lines (``One more``, ``Cmp ...``) are ignored.

Parsing is pure regex — no AI/LLM — and, like the other parsers, rejects anything
that does not clearly match by returning ``None``. This channel is **parse-only**
for now (stored + logged, not traded); the produced :class:`Signal` reuses the
existing shape so a future trade path can consume it unchanged.
"""

from __future__ import annotations

import re

from .logging_config import get_logger
from .parser import Action, OptionType, Signal

__all__ = ["looks_like_signal", "parse_channel2_signal"]

logger = get_logger(__name__)

_MAX_LOGGED_CHARS = 120

# A bare number with an optional decimal part (shared convention with the other
# parsers).
_NUM = r"\d+(?:\.\d+)?"

# Header line: "<underlying> <strike> <ce|pe> above <entry>". The underlying is
# non-greedy so it stops at the strike; it may be multi-word (stock names). Entry
# follows "above" (breakout) or occasionally "at"; a trailing range ("at 159-155")
# is tolerated and only the first number is taken as the entry.
_HEADER_RE = re.compile(
    rf"^(?P<underlying>.+?)\s+"
    rf"(?P<strike>\d+)\s+"
    rf"(?P<option_type>ce|pe)\s+"
    rf"(?:above|at)\s+"
    rf"(?P<entry>{_NUM})"
    rf"(?:\s*[-–]\s*{_NUM})?$",
    re.IGNORECASE,
)

# Field lines, matched anywhere in the message regardless of order. A stray
# leading dot (".lot 65", seen in real messages) is tolerated.
_SL_RE = re.compile(rf"^\.?\s*SL\s+(?P<sl>{_NUM})$", re.IGNORECASE)
_TARGET_RE = re.compile(
    rf"^\.?\s*TARGET\s+(?P<targets>{_NUM}(?:\s*,\s*{_NUM})*)\s*(?P<open>\++)?$",
    re.IGNORECASE,
)


def parse_channel2_signal(message: str | None) -> Signal | None:
    """Parse a channel-2 message into a :class:`Signal`, or ``None`` if it is not
    a valid entry signal.

    A valid signal needs the header line (underlying/strike/type/entry) plus a
    numeric ``Sl`` and at least one ``Target``; the first target is used, raw.
    Missing either the stop or a target is a rejection (better a miss than a bad
    parse). ``Lot``/``Cmp`` and any extra chatter lines are ignored. Rejection is
    graceful — noise yields ``None`` (logged at DEBUG), never an exception.
    """
    if not message or not message.strip():
        return None

    lines = [line.strip() for line in message.strip().splitlines()]
    lines = [line for line in lines if line]

    header_match: re.Match[str] | None = None
    sl: float | None = None
    first_target: float | None = None
    target_open_ended = False

    for line in lines:
        if header_match is None and (match := _HEADER_RE.match(line)):
            header_match = match
            continue
        if sl is None and (match := _SL_RE.match(line)):
            sl = float(match.group("sl"))
            continue
        if first_target is None and (match := _TARGET_RE.match(line)):
            first_target = float(match.group("targets").split(",")[0].strip())
            target_open_ended = bool(match.group("open"))

    if header_match is None or sl is None or first_target is None:
        return _reject(message, "not a channel-2 signal (need header + Sl + Target)")

    signal = Signal(
        underlying=header_match.group("underlying").strip().upper(),
        strike=int(header_match.group("strike")),
        option_type=OptionType(header_match.group("option_type").upper()),
        action=Action.BUY,
        entry_price=float(header_match.group("entry")),
        stop_loss=sl,
        target=first_target,
        target_open_ended=target_open_ended,
        raw_text=message,
    )
    logger.info("Parsed channel-2 signal: %s", signal)
    return signal


def _reject(message: str, reason: str) -> None:
    """Log a rejected message at DEBUG and return ``None`` (see the sibling parsers)."""
    snippet = message.strip().replace("\n", " ⏎ ")
    if len(snippet) > _MAX_LOGGED_CHARS:
        snippet = snippet[:_MAX_LOGGED_CHARS] + "…"
    logger.debug("Rejected channel-2 message (%s): %r", reason, snippet)
    return None


# --- Missed-signal detection ------------------------------------------------
#
# The parser deliberately rejects anything it cannot read exactly, which protects
# the account but hides a real risk: this channel's formatting is loose, so a
# genuine trade can be missed in silence. These patterns pick out messages that
# *look* like an entry — an option contract, or the stop/target labels — so a
# miss can be surfaced for review while ordinary chatter stays quiet.

#: An option contract mentioned anywhere: "Coforge 1500 ce", "23900 PE".
_CONTRACT_MENTION_RE = re.compile(r"\b\d{2,6}\s*(?:ce|pe)\b", re.IGNORECASE)

#: The labels an entry carries. A message with a stop or a target is proposing a
#: trade, whatever shape the rest of it is in.
_ENTRY_LABEL_RE = re.compile(r"^\.?\s*(?:sl|target)\b", re.IGNORECASE | re.MULTILINE)

#: Commentary that routinely mentions a contract without proposing an entry:
#: outcome narration ("target done", "sl hit"), price pings, and cancellations.
#: These are expected noise and must not be reported as missed signals.
_NARRATION_RE = re.compile(
    r"target\s*done|safe\s*target|book(?:ed)?\s*(?:profit|partial)|"
    r"sl\s*hit|stop\s*hit|hit\s*sl|trail|exit(?:ed)?|"
    r"type\s*mistake|ignore|cancel|closed|c\s*to\s*c|"
    r"holding|running|cmp\s+" + _NUM,
    re.IGNORECASE,
)


def looks_like_signal(message: str | None) -> bool:
    """Whether ``message`` looks like an entry the parser *should* have read.

    Used to tell a genuine formatting miss apart from the channel's ordinary
    chatter, so the user is alerted about the former and spared the latter. It is
    intentionally a heuristic, and a loose one: a false alert costs a glance at a
    phone, while a missed signal costs a trade. Narration about trades that
    already exist (targets done, stop hit, cancellations, price pings) is excluded
    even though it mentions contracts.

    Only meaningful for messages the parser rejected — a parsed signal is not a
    miss.
    """
    if not message or not message.strip():
        return False
    text = message.strip()
    if _NARRATION_RE.search(text):
        return False
    return bool(_CONTRACT_MENTION_RE.search(text) or _ENTRY_LABEL_RE.search(text))
