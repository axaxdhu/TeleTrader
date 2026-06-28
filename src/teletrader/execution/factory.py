"""Executor selection from configuration.

The composition root asks for *an* :class:`Executor` and gets the one named by
``EXECUTION_MODE`` — ``dry_run`` today, ``kite`` later. Putting the choice here
(not in the engine or the listener) means switching from a dry run to live
trading is a one-line config change and nothing upstream knows the difference.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from ..config import Config
from .base import Executor
from .dry_run import MODE as DRY_RUN_MODE
from .dry_run import DryRunExecutor
from .kite import MODE as KITE_MODE
from .kite import KiteExecutor
from .repository import ExecutionRepository

__all__ = ["KITE_MODE", "create_executor"]


def create_executor(
    config: Config,
    repository: ExecutionRepository,
    *,
    clock: Callable[[], datetime] | None = None,
) -> Executor:
    """Build the :class:`Executor` named by ``config.execution_mode``.

    This is the only place the config string maps to a class, so switching
    between a dry run and live Kite trading is purely a configuration change.
    Raises :class:`ValueError` for anything unrecognised — though config
    validation should reject unknown modes before this is reached.
    """
    mode = config.execution_mode
    if mode == DRY_RUN_MODE:
        return DryRunExecutor(repository, clock=clock)
    if mode == KITE_MODE:
        return KiteExecutor(
            repository,
            api_key=config.kite_api_key,
            access_token=config.kite_access_token,
            tz=ZoneInfo(config.market_timezone),
            clock=clock,
        )
    raise ValueError(f"Unknown execution mode: {mode!r}")
