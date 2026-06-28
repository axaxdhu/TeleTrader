"""Execution exception hierarchy.

A single family of errors the execution layer raises, so callers handle failures
uniformly without knowing which executor (dry-run or Kite) is behind the
interface. The base is :class:`ExecutionError` (project convention is ``...Error``);
``except ExecutionError`` catches anything the layer can throw.

``InvalidOrderError`` is raised by request validation and is the only one the
:class:`~teletrader.execution.dry_run.DryRunExecutor` deals in. The remaining
errors are how the :class:`~teletrader.execution.kite.KiteExecutor` *translates*
the Kite SDK's native exceptions into broker-neutral categories, so nothing
vendor-specific leaks upward. The executor turns each of these into a structured
:class:`~teletrader.execution.models.ExecutionResult` rather than letting it crash
the application; the classes give that translation clear, testable categories.

Two axes describe a failure:

* **Rejected vs failed** — :class:`OrderRejectedError` (and its
  :class:`InsufficientMarginError` subclass) mean the broker declined the *order*
  for a business reason → ``REJECTED``. The others (auth, transport, rate limit)
  mean the *call* could not be completed → ``FAILED``.
"""

from __future__ import annotations

__all__ = [
    "AuthenticationError",
    "BrokerCommunicationError",
    "ExecutionError",
    "InstrumentNotFoundError",
    "InsufficientMarginError",
    "InvalidOrderError",
    "OrderRejectedError",
    "RateLimitError",
]


class ExecutionError(Exception):
    """Base class for every error raised by the execution layer."""


class InvalidOrderError(ExecutionError):
    """The order request failed validation and was never submitted.

    Raised *before* any broker call (e.g. non-positive quantity, missing symbol,
    a LIMIT order with no price).
    """


class OrderRejectedError(ExecutionError):
    """A broker accepted the request but rejected the order.

    A business-level rejection (invalid symbol, market closed, frozen quantity,
    a plain rejection) — the call reached the broker but the order will not be
    placed. Maps to ``REJECTED``.
    """


class InsufficientMarginError(OrderRejectedError):
    """The account lacked the margin/funds required for the order (``REJECTED``)."""


class InstrumentNotFoundError(OrderRejectedError):
    """No tradeable broker contract matched the order (``REJECTED``).

    Raised by symbol resolution when the (underlying, strike, option type) has no
    contract expiring on or after the order date in the broker's instrument master
    — so there is nothing to submit.
    """


class AuthenticationError(ExecutionError):
    """The Kite access token was missing, invalid, or expired (``FAILED``).

    Authentication is manual (a valid ``KITE_ACCESS_TOKEN`` is assumed); this
    signals that token must be regenerated. No login is automated by this app.
    """


class BrokerCommunicationError(ExecutionError):
    """The broker could not be reached / the call failed in transit (``FAILED``)."""


class RateLimitError(BrokerCommunicationError):
    """The broker throttled the request (HTTP 429 / too many requests) (``FAILED``)."""
