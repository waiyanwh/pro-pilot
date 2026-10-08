"""Per-call budget and pagination metadata shared with blocking read clients."""

from __future__ import annotations

import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

DEADLINE: ContextVar[float | None] = ContextVar("spoke_deadline", default=None)
TRUNCATED: ContextVar[bool] = ContextVar("spoke_read_truncated", default=False)


@dataclass(frozen=True)
class ExecutionResult:
    data: Any
    truncated: bool = False


def remaining_seconds(default: float = 15) -> float:
    deadline = DEADLINE.get()
    remaining = default if deadline is None else min(default, deadline - time.monotonic())
    if remaining <= 0:
        raise TimeoutError("tool deadline exceeded")
    return remaining


def mark_truncated() -> None:
    TRUNCATED.set(True)
