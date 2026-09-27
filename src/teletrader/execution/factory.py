"""Executor selection from configuration.

The composition root asks for *an* :class:`Executor` for a given broker mode and
gets the matching implementation — ``dry_run``, ``kite``, ``fyers``, or
``fyers_shadow``. Putting
the choice here (not in the engine or the listener) means switching a channel's
broker is a one-line config change and nothing upstream knows the difference.

The mode is passed explicitly so each channel can be built with its own broker
(``Config.broker_for(channel)``); it defaults to the global ``execution_mode`` for
single-broker setups.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from ..config import Config
from .base import Executor
from .dry_run import MODE as DRY_RUN_MODE
from .dry_run import DryRunExecutor
from .fyers import MODE as FYERS_MODE
from .fyers import FyersExecutor
from .kite import MODE as KITE_MODE
from .kite import KiteExecutor
from .repository import ExecutionRepository
from .shadow import MODE as FYERS_SHADOW_MODE
from .shadow import FyersShadowExecutor

__all__ = [
    "DRY_RUN_MODE",
    "FYERS_MODE",
    "FYERS_SHADOW_MODE",
    "KITE_MODE",
    "create_executor",
]


def create_executor(
    config: Config,
    repository: ExecutionRepository,
    *,
    mode: str | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Executor:
    """Build the :class:`Executor` for ``mode`` (default: ``config.execution_mode``).

    This is the only place a broker mode maps to a class, so switching a channel
    between a dry run and a live broker is purely a configuration change. Raises
    :class:`ValueError` for anything unrecognised — though config validation should
    reject unknown modes before this is reached.
    """
    resolved = mode or config.execution_mode
    if resolved == DRY_RUN_MODE:
        return DryRunExecutor(repository, clock=clock)
    if resolved == KITE_MODE:
        return KiteExecutor(
            repository,
            api_key=config.kite_api_key,
            access_token=config.kite_access_token,
            tz=ZoneInfo(config.market_timezone),
            clock=clock,
        )
    if resolved in (FYERS_MODE, FYERS_SHADOW_MODE):
        # Shadow mode is the live executor minus the final submit, so it is built
        # from exactly the same credentials and symbol master.
        executor_class = (
            FyersExecutor if resolved == FYERS_MODE else FyersShadowExecutor
        )
        return executor_class(
            repository,
            app_id=config.fyers_app_id,
            access_token=config.fyers_access_token,
            tz=ZoneInfo(config.market_timezone),
            clock=clock,
        )
    raise ValueError(f"Unknown execution mode: {resolved!r}")
