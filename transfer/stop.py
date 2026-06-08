"""Stop/cancel helpers for long-running transfer operations."""

from __future__ import annotations

from collections.abc import Callable

ShouldStop = Callable[[], bool] | None


def is_cancelled(should_stop: ShouldStop) -> bool:
    return bool(should_stop and should_stop())
