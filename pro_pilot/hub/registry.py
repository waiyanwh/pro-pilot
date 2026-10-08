"""Connected spoke registry, authentication, and tool-call correlation."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from websockets.exceptions import ConnectionClosed

from pro_pilot.protocol.messages import CloseCode, ErrorCode, Hello, ToolCall, ToolResult
from pro_pilot.protocol.tools import TOOL_REGISTRY

LOGGER = logging.getLogger(__name__)


class RegistryError(RuntimeError):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class SpokeConnection:
    websocket: Any
    hello: Hello
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    connected_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    pending: dict[str, asyncio.Future[ToolResult]] = field(default_factory=dict)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    inflight: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(16))

    async def send_call(self, call: ToolCall) -> ToolResult:
        try:
            async with asyncio.timeout(call.deadline_ms / 1000), self.inflight:
                future: asyncio.Future[ToolResult] = asyncio.get_running_loop().create_future()
                self.pending[call.id] = future
                try:
                    async with self.send_lock:
                        await self.websocket.send(call.model_dump_json(exclude_none=True))
                    return await future
                finally:
                    self.pending.pop(call.id, None)
                    if not future.done():
                        future.cancel()
        except TimeoutError as exc:
            raise RegistryError(ErrorCode.TIMEOUT, "spoke tool call timed out") from exc
        except (ConnectionClosed, ConnectionError, OSError) as exc:
            raise RegistryError(ErrorCode.CLUSTER_UNAVAILABLE, "spoke disconnected") from exc

    def resolve(self, result: ToolResult) -> None:
        self.last_seen = time.time()
        future = self.pending.pop(result.id, None)
        if future is None:
            LOGGER.warning("spoke_unknown_tool_result", extra={"id": result.id})
            return
        if not future.done():
            future.set_result(result)

    def fail_pending(self) -> None:
        for call_id, future in list(self.pending.items()):
            self.pending.pop(call_id, None)
            if not future.done():
                future.set_exception(
                    RegistryError(ErrorCode.CLUSTER_UNAVAILABLE, "spoke disconnected mid-call")
                )


class SpokeRegistry:
    def __init__(self, token_hashes: dict[str, list[str]] | None = None) -> None:
        self.token_hashes = token_hashes or {}
        self._connections: dict[str, SpokeConnection] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def hash_token(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def authenticate(self, cluster_id: str, token: str) -> bool:
        presented = self.hash_token(token)
        for configured in self.token_hashes.get(cluster_id, []):
            expected = configured.removeprefix("sha256:")
            if hmac.compare_digest(presented, expected):
                return True
        return False

    async def register(self, hello: Hello, websocket: Any) -> SpokeConnection:
        known_tools = [
            name
            for name in hello.tools
            if name in TOOL_REGISTRY and not TOOL_REGISTRY[name].hub_local
        ]
        for _name in set(hello.tools) - set(known_tools):
            LOGGER.warning("spoke_unknown_tool", extra={"cluster_id": hello.cluster_id})
        hello = hello.model_copy(update={"tools": known_tools})
        connection = SpokeConnection(websocket=websocket, hello=hello)
        async with self._lock:
            previous = self._connections.get(hello.cluster_id)
            self._connections[hello.cluster_id] = connection
        LOGGER.info("spoke_connected", extra={"cluster_id": hello.cluster_id})
        if previous is not None:
            previous.fail_pending()
            await _close_quietly(
                previous.websocket, CloseCode.REPLACED, "replaced by newer connection"
            )
        return connection

    async def disconnect(self, connection: SpokeConnection) -> None:
        connection.fail_pending()
        async with self._lock:
            if self._connections.get(connection.hello.cluster_id) is connection:
                self._connections.pop(connection.hello.cluster_id, None)
                LOGGER.info("spoke_disconnected", extra={"cluster_id": connection.hello.cluster_id})

    def get(self, cluster_id: str) -> SpokeConnection | None:
        return self._connections.get(cluster_id)

    def statuses(self, cluster_ids: list[str] | None = None) -> list[dict[str, Any]]:
        ids = cluster_ids if cluster_ids is not None else sorted(self.token_hashes)
        result = []
        for cluster_id in ids:
            connection = self._connections.get(cluster_id)
            if connection is None:
                result.append(
                    {
                        "cluster_id": cluster_id,
                        "connected": False,
                        "labels": {},
                        "capabilities": [],
                    }
                )
            else:
                result.append(
                    {
                        "cluster_id": cluster_id,
                        "connected": True,
                        "labels": connection.hello.labels,
                        "capabilities": connection.hello.capabilities,
                    }
                )
        return result

    async def call(
        self,
        *,
        cluster_id: str,
        tool: str,
        args: dict[str, Any],
        trace_id: str,
        deadline_ms: int,
    ) -> ToolResult:
        connection = self._connections.get(cluster_id)
        if connection is None:
            raise RegistryError(ErrorCode.CLUSTER_UNAVAILABLE, f"cluster {cluster_id} is offline")
        if tool not in connection.hello.tools:
            raise RegistryError(
                ErrorCode.TOOL_NOT_SUPPORTED, f"tool {tool} is not supported by the spoke"
            )
        call = ToolCall(
            id=str(uuid.uuid4()),
            trace_id=trace_id,
            tool=tool,
            args=args,
            deadline_ms=deadline_ms,
        )
        try:
            return await connection.send_call(call)
        except RegistryError:
            raise
        except Exception as exc:  # noqa: BLE001 - connection boundary
            raise RegistryError(ErrorCode.CLUSTER_UNAVAILABLE, "spoke disconnected") from exc


async def _close_quietly(websocket: Any, code: int, reason: str) -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except Exception:  # noqa: BLE001 - close is best effort
        LOGGER.debug("spoke_close_failed", exc_info=True)
