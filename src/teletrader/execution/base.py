"""The :class:`Executor` interface — the seam between the Trade Engine and a broker.

This is the abstraction the rest of the system depends on. The Trade Engine
decides *whether* to trade; an `Executor` is responsible for *submitting* the
resulting order — and the engine (and any caller) only ever sees this interface,
never a broker SDK. Two implementations are interchangeable behind it::

    Trade Engine
        │  OrderRequest
        ▼
    Executor  (this interface)
        ├── DryRunExecutor   — validates + logs + records; submits nothing
        └── KiteExecutor     — submits to Zerodha Kite (next phase)

Selecting which one runs is pure configuration (``EXECUTION_MODE``); see
:func:`teletrader.execution.factory.create_executor`. The interface is
deliberately small (one method): an executor *executes an order*. It does not
model positions, fills, or P&L — that is out of scope by design.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .models import ExecutionResult, OrderRequest

__all__ = ["Executor"]


class Executor(ABC):
    """Submits orders on behalf of the Trade Engine.

    Implementations translate an :class:`OrderRequest` into whatever their target
    requires (a log line for the dry run; a Kite API call later) and return an
    :class:`ExecutionResult` describing the attempt.
    """

    @property
    @abstractmethod
    def mode(self) -> str:
        """The configured mode this executor implements (e.g. ``"dry_run"``)."""

    @abstractmethod
    def execute(self, order: OrderRequest) -> ExecutionResult:
        """Attempt to submit ``order`` and return the :class:`ExecutionResult`.

        Implementations validate the request, record the attempt, and either
        report success (``SUCCESS``) or rejection (``REJECTED``). A live executor
        may raise an :class:`~teletrader.execution.exceptions.ExecutionError`
        subclass if the broker call itself fails.
        """
