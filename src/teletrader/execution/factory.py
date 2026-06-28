"""Executor selection from configuration.

The composition root asks for *an* :class:`Executor` and gets the one named by
``EXECUTION_MODE`` — ``dry_run`` today, ``kite`` later. Putting the choice here
(not in the engine or the listener) means switching from a dry run to live
trading is a one-line config change and nothing upstream knows the difference.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from ..config import Config
from .base import Executor
from .dry_run import MODE as DRY_RUN_MODE
from .dry_run import DryRunExecutor
from .repository import ExecutionRepository

__all__ = ["KITE_MODE", "create_executor"]

#: The (not-yet-implemented) live mode. Named here so config validation and the
#: factory agree on the spelling; the executor itself arrives in the next phase.
KITE_MODE = "kite"


def create_executor(
    config: Config,
    repository: ExecutionRepository,
    *,
    clock: Callable[[], datetime] | None = None,
) -> Executor:
    """Build the :class:`Executor` named by ``config.execution_mode``.

    Raises :class:`NotImplementedError` for ``kite`` (next phase) and
    :class:`ValueError` for anything unrecognised — though config validation
    should reject unknown modes before this is reached.
    """
    mode = config.execution_mode
    if mode == DRY_RUN_MODE:
        return DryRunExecutor(repository, clock=clock)
    if mode == KITE_MODE:
        raise NotImplementedError(
            "KiteExecutor is not implemented yet; set EXECUTION_MODE=dry_run"
        )
    raise ValueError(f"Unknown execution mode: {mode!r}")
