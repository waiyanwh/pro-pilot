"""Provider-independent model failure and async invocation helpers."""

from __future__ import annotations

import inspect
from typing import Any


class LLMUnavailable(RuntimeError):
    """The configured model could not produce a valid response."""


async def maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value
