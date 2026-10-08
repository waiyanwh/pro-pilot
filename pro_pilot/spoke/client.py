"""Outbound WebSocket client for a read-only spoke."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from pro_pilot.protocol.messages import (
    CloseCode,
    Hello,
    ProtocolError,
    ToolCall,
    Welcome,
    encode_message,
    parse_message,
)

from .pipeline import ToolPipeline

LOGGER = logging.getLogger(__name__)


class ProtocolUnsupported(RuntimeError):
    """The hub rejected this spoke's protocol major version."""


class SpokeClient:
    def __init__(
        self,
        *,
        url: str,
        token: str,
        hello: Hello,
        pipeline: ToolPipeline,
        heartbeat_s: int = 20,
        random_fn: Callable[[], float] = random.random,
        prepare: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.url = url
        self.token = token
        self.hello = hello
        self.pipeline = pipeline
        self.heartbeat_s = heartbeat_s
        self.random_fn = random_fn
        self._prepare = prepare
        self._stop = asyncio.Event()
        self._websocket: Any = None
        self._connected = False
        self._tasks: set[asyncio.Task[None]] = set()

    async def is_connected(self) -> bool:
        return self._connected

    async def run_forever(self) -> None:
        """Connect and reconnect until closed, or exit on unsupported protocol."""

        attempt = 0
        while not self._stop.is_set():
            stable_started = asyncio.get_running_loop().time()
            code: int | None = None
            try:
                await self.run_once()
            except ProtocolUnsupported:
                raise
            except ConnectionClosed as exc:
                code = exc.rcvd.code if exc.rcvd is not None else None
                if code == CloseCode.PROTOCOL_UNSUPPORTED:
                    raise ProtocolUnsupported("hub does not support this protocol") from exc
                LOGGER.error("spoke_connection_closed", extra={"code": code})
            except (OSError, TimeoutError, websockets.WebSocketException, ProtocolError) as exc:
                LOGGER.error("spoke_connection_error", extra={"error_type": type(exc).__name__})
            if self._stop.is_set():
                break
            if asyncio.get_running_loop().time() - stable_started >= 60:
                attempt = 0
            await self._sleep_or_stop(self._close_delay(code, attempt))
            attempt += 1

    async def run_once(self) -> None:
        if self._prepare is not None:
            await self._prepare()
        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-Pro-Pilot-Cluster": self.hello.cluster_id,
            "User-Agent": f"pro-pilot-spoke/{self.hello.spoke_version}",
        }
        async with websockets.connect(
            self.url,
            additional_headers=headers,
            max_size=1024 * 1024,
            ping_interval=self.heartbeat_s,
            ping_timeout=self.heartbeat_s * 2,
        ) as websocket:
            self._websocket = websocket
            try:
                await websocket.send(encode_message(self.hello))
                welcome = await asyncio.wait_for(websocket.recv(), timeout=10)
                message = parse_message(welcome)
                if not isinstance(message, Welcome):
                    raise ProtocolError("hub did not send welcome")
                self._connected = True
                async for frame in websocket:
                    incoming = parse_message(frame)
                    if isinstance(incoming, ToolCall):
                        if len(self._tasks) >= 16:
                            await asyncio.wait(self._tasks, return_when=asyncio.FIRST_COMPLETED)
                        task = asyncio.create_task(self._handle_tool_call(incoming))
                        self._tasks.add(task)
                        task.add_done_callback(self._task_done)
            finally:
                self._connected = False
                await self._cancel_tasks()
                self._websocket = None

    async def _handle_tool_call(self, call: ToolCall) -> None:
        result = await self.pipeline.execute(
            call.id,
            call.tool,
            call.args,
            deadline_ms=call.deadline_ms,
            trace_id=call.trace_id,
        )
        if self._websocket is not None:
            await self._websocket.send(encode_message(result))

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            LOGGER.error("spoke_tool_send_failed", extra={"error_type": type(error).__name__})

    async def close(self) -> None:
        self._stop.set()
        self._connected = False
        if self._websocket is not None:
            await self._websocket.close(code=CloseCode.NORMAL)
        await self._cancel_tasks()

    async def _cancel_tasks(self) -> None:
        tasks = tuple(self._tasks)
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _backoff_delay(self, attempt: int) -> float:
        base = min(60, 2 ** min(attempt, 6))
        return base * (0.8 + self.random_fn() * 0.4)

    def _close_delay(self, code: int | None, attempt: int) -> float:
        if code == CloseCode.AUTH_FAILED:
            LOGGER.error("spoke_auth_rejected")
            return 300
        if code == CloseCode.REPLACED:
            return 30
        return self._backoff_delay(attempt)

    async def _sleep_or_stop(self, delay: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
