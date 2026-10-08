"""Graceful CLI shutdown so pod termination reaches transport and client cleanup."""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Coroutine
from typing import Any


async def run_with_shutdown(coroutine: Coroutine[Any, Any, None]) -> None:
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(coroutine)
    stopping = False

    def stop() -> None:
        nonlocal stopping
        stopping = True
        task.cancel()

    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop)
    try:
        await task
    except asyncio.CancelledError:
        if not stopping:
            raise
    finally:
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)
