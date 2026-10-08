"""Hub WebSocket server for authenticated outbound spoke connections."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from pro_pilot.protocol.messages import (
    CloseCode,
    Hello,
    ProtocolError,
    ToolResult,
    Welcome,
    encode_message,
    parse_message,
)
from pro_pilot.protocol.version import SUPPORTED_PROTOCOL_VERSIONS

from .registry import SpokeConnection, SpokeRegistry

LOGGER = logging.getLogger(__name__)


class SpokeServer:
    def __init__(
        self,
        registry: SpokeRegistry,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        path: str = "/spoke/v1/connect",
        heartbeat_s: int = 20,
    ) -> None:
        self.registry = registry
        self.host = host
        self.port = port
        self.path = path
        self.heartbeat_s = heartbeat_s
        self._server: Any = None

    async def start(self) -> Any:
        self._server = await websockets.serve(
            self.handler,
            self.host,
            self.port,
            max_size=1024 * 1024,
            ping_interval=self.heartbeat_s,
            ping_timeout=self.heartbeat_s * 2,
        )
        return self._server

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def handler(self, websocket: Any) -> None:
        request = getattr(websocket, "request", None)
        headers = request.headers if request is not None else websocket.request_headers
        request_path = request.path if request is not None else getattr(websocket, "path", "")
        if request_path != self.path:
            await websocket.close(code=CloseCode.BAD_HELLO, reason="invalid spoke path")
            return
        cluster_id = headers.get("X-Pro-Pilot-Cluster", "")
        authorization = headers.get("Authorization", "")
        token = authorization.removeprefix("Bearer ").strip()
        if (
            not authorization.startswith("Bearer ")
            or not cluster_id
            or not self.registry.authenticate(cluster_id, token)
        ):
            LOGGER.error(
                "spoke_auth_failed", extra={"cluster_id": cluster_id, "peer": self._peer(websocket)}
            )
            await websocket.close(code=CloseCode.AUTH_FAILED, reason="authentication failed")
            return
        connection: SpokeConnection | None = None
        try:
            frame = await asyncio.wait_for(websocket.recv(), timeout=10)
            if isinstance(frame, bytes):
                await websocket.close(code=CloseCode.BAD_HELLO, reason="binary hello")
                return
            try:
                hello = parse_message(frame)
            except ProtocolError as exc:
                code = (
                    CloseCode.PROTOCOL_UNSUPPORTED
                    if str(exc) == "unsupported protocol version"
                    else CloseCode.BAD_HELLO
                )
                await websocket.close(code=code, reason="invalid hello")
                return
            if not isinstance(hello, Hello):
                await websocket.close(
                    code=CloseCode.BAD_HELLO, reason="first message must be hello"
                )
                return
            if hello.v not in SUPPORTED_PROTOCOL_VERSIONS or hello.cluster_id != cluster_id:
                code = (
                    CloseCode.PROTOCOL_UNSUPPORTED
                    if hello.v not in SUPPORTED_PROTOCOL_VERSIONS
                    else CloseCode.BAD_HELLO
                )
                await websocket.close(code=code, reason="hello mismatch")
                return
            connection = await self.registry.register(hello, websocket)
            await websocket.send(
                encode_message(
                    Welcome(
                        session_id=str(uuid.uuid4()),
                        heartbeat_s=self.heartbeat_s,
                    )
                )
            )
            async for incoming in websocket:
                message = parse_message(incoming)
                if isinstance(message, ToolResult):
                    connection.resolve(message)
        except TimeoutError:
            await websocket.close(code=CloseCode.HELLO_TIMEOUT, reason="hello timeout")
        except ConnectionClosed:
            pass
        except ProtocolError:
            await websocket.close(code=CloseCode.BAD_HELLO, reason="invalid protocol message")
        finally:
            if connection is not None:
                await self.registry.disconnect(connection)

    @staticmethod
    def _peer(websocket: Any) -> str:
        peer = getattr(websocket, "remote_address", None)
        if isinstance(peer, tuple):
            return str(peer[0])
        return str(peer or "unknown")
