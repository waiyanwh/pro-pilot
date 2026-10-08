from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pro_pilot.hub.registry import RegistryError, SpokeConnection
from pro_pilot.protocol.messages import ErrorCode, Hello, ToolCall


class BlockedSocket:
    def __init__(self) -> None:
        self.sending = asyncio.Event()

    async def send(self, payload: str) -> None:
        self.sending.set()
        await asyncio.Event().wait()


def connection() -> SpokeConnection:
    return SpokeConnection(
        websocket=BlockedSocket(),
        hello=Hello(cluster_id="test-cluster", spoke_version="0.1.0"),
    )


def call() -> ToolCall:
    return ToolCall(id="call", trace_id="trace", tool="get_pods", deadline_ms=20)


@pytest.mark.parametrize("blocked_at", ["queue", "send_lock", "send"])
async def test_request_deadline_includes_queue_and_send(blocked_at: str) -> None:
    spoke = connection()
    blocker: Any = None
    if blocked_at == "queue":
        spoke.inflight = asyncio.Semaphore(0)
    elif blocked_at == "send_lock":
        blocker = spoke.send_lock
        await blocker.acquire()
    try:
        with pytest.raises(RegistryError) as caught:
            await asyncio.wait_for(spoke.send_call(call()), timeout=0.5)
        assert caught.value.code == ErrorCode.TIMEOUT
        assert spoke.pending == {}
    finally:
        if blocker is not None:
            blocker.release()


async def test_cancellation_removes_pending_call_and_releases_slot() -> None:
    spoke = connection()
    task = asyncio.create_task(spoke.send_call(call().model_copy(update={"deadline_ms": 1000})))
    await asyncio.wait_for(spoke.websocket.sending.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert spoke.pending == {}
    await asyncio.wait_for(spoke.inflight.acquire(), timeout=0.2)
    spoke.inflight.release()
