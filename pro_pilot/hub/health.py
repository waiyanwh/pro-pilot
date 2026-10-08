"""Process liveness and live Socket Mode readiness, without model calls."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from aiohttp import web


class HealthServer:
    def __init__(
        self,
        connected: Callable[[], Awaitable[bool]],
        *,
        host: str,
        port: int,
        disconnected_status: str = "slack_disconnected",
    ) -> None:
        self.connected = connected
        self.host = host
        self.port = port
        self.disconnected_status = disconnected_status
        self.app = web.Application()
        self.app.router.add_get("/healthz", self.health)
        self.app.router.add_get("/readyz", self.ready)
        self._runner: web.AppRunner | None = None

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "alive"})

    async def ready(self, request: web.Request) -> web.Response:
        connected = await self.connected()
        return web.json_response(
            {"status": "ready" if connected else self.disconnected_status},
            status=200 if connected else 503,
        )

    async def start(self) -> None:
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        await web.TCPSite(self._runner, self.host, self.port).start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
