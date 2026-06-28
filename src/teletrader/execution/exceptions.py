"""Execution exception hierarchy.

A single family of errors the execution layer raises, so callers handle failures
uniformly without knowing which executor (dry-run or, later, Kite) is behind the
interface. The base is :class:`ExecutionError` (project convention is ``...Error``);
``except ExecutionError`` catches anything the layer can throw.

``InvalidOrderError`` is raised by request validation and is the only one the
:class:`~teletrader.execution.dry_run.DryRunExecutor` deals in. The transport
errors exist for the future ``KiteExecutor`` to translate the broker SDK's native
failures into, so nothing vendor-specific leaks upward.
"""

from __future__ import annotations

__all__ = [
    "BrokerCommunicationError",
    "ExecutionError",
    "InvalidOrderError",
    "OrderRejectedError",
]


class ExecutionError(Exception):
    """Base class for every error raised by the execution layer."""


class InvalidOrderError(ExecutionError):
    """The order request failed validation and was never submitted.

    Raised *before* any broker call (e.g. non-positive quantity, missing symbol,
    a LIMIT order with no price).
    """


class OrderRejectedError(ExecutionError):
    """A broker accepted the request but rejected the order (future live executor)."""


class BrokerCommunicationError(ExecutionError):
    """The broker could not be reached / the call failed (future live executor)."""
