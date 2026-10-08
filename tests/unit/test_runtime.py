from __future__ import annotations

import asyncio
import signal
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from pro_pilot.hub.health import HealthServer
from pro_pilot.hub.main import load_config as load_hub_config
from pro_pilot.hub.registry import SpokeRegistry
from pro_pilot.hub.spoke_server import SpokeServer
from pro_pilot.runtime import run_with_shutdown
from pro_pilot.spoke.main import build_client
from pro_pilot.spoke.main import load_config as load_spoke_config


async def test_readiness_tracks_live_slack_connection() -> None:
    state = {"connected": False}

    async def connected() -> bool:
        return state["connected"]

    health = HealthServer(connected, host="127.0.0.1", port=0)
    async with TestClient(TestServer(health.app)) as client:
        alive = await client.get("/healthz")
        assert alive.status == 200
        assert await alive.json() == {"status": "alive"}
        unavailable = await client.get("/readyz")
        assert unavailable.status == 503
        assert await unavailable.json() == {"status": "slack_disconnected"}
        state["connected"] = True
        ready = await client.get("/readyz")
        assert ready.status == 200
        assert await ready.json() == {"status": "ready"}
        state["connected"] = False
        assert (await client.get("/readyz")).status == 503


async def test_environment_overrides_yaml_without_loading_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub = tmp_path / "hub.yaml"
    hub.write_text("llm:\n  mode: fake\nagent:\n  max_tool_calls: 16\n", encoding="utf-8")
    (tmp_path / ".env").write_text("PRO_PILOT_LLM_MODE=unexpected\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PRO_PILOT_LLM_MODE", raising=False)
    monkeypatch.setenv("PRO_PILOT_AGENT__MAX_TOOL_CALLS", "2")
    settings = load_hub_config(hub)
    assert settings.agent.max_tool_calls == 2
    assert settings.llm_mode == "fake"

    spoke = tmp_path / "spoke.yaml"
    spoke.write_text(
        "cluster_id: test-cluster\nfixture_scenario: cap\nlimits:\n  max_list_items: 200\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PRO_PILOT_LIMITS__MAX_LIST_ITEMS", "3")
    fixtures = tmp_path / "fixtures" / "cap"
    fixtures.mkdir(parents=True)
    (fixtures / "responses.json").write_text(
        '{"list_namespaces":[{"name":"one"},{"name":"two"},{"name":"three"},{"name":"four"}]}',
        encoding="utf-8",
    )
    client = build_client(
        load_spoke_config(spoke), token="test-placeholder", fixture_root=fixtures.parent
    )
    result = await client.pipeline.execute("list", "list_namespaces", {}, deadline_ms=1000)
    assert result.ok and result.truncated
    assert isinstance(result.data, list)
    assert [namespace["name"] for namespace in result.data] == ["one", "two", "three"]


async def test_spoke_readiness_requires_authenticated_welcome_and_clears_on_disconnect(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "scenario"
    fixture.mkdir()
    (fixture / "responses.json").write_text('{"list_namespaces": []}')
    registry = SpokeRegistry({"test-cluster": [SpokeRegistry.hash_token("synthetic-token")]})
    server = SpokeServer(registry, host="127.0.0.1", port=0)
    await server.start()
    port = server._server.sockets[0].getsockname()[1]
    spoke = build_client(
        {
            "cluster_id": "test-cluster",
            "fixture_scenario": "scenario",
            "hub": {"url": f"ws://127.0.0.1:{port}/spoke/v1/connect"},
        },
        token="synthetic-token",
        fixture_root=tmp_path,
    )
    health = HealthServer(
        spoke.is_connected, host="127.0.0.1", port=0, disconnected_status="hub_disconnected"
    )
    task = None
    try:
        async with TestClient(TestServer(health.app)) as http:
            not_ready = await http.get("/readyz")
            assert not_ready.status == 503
            assert await not_ready.json() == {"status": "hub_disconnected"}
            assert (await http.get("/healthz")).status == 200
            task = asyncio.create_task(spoke.run_once())
            for _ in range(100):
                if await spoke.is_connected():
                    break
                await asyncio.sleep(0.01)
            assert await spoke.is_connected()
            assert (await http.get("/readyz")).status == 200
            await spoke.close()
            await task
            assert (await http.get("/readyz")).status == 503
            assert (await http.get("/healthz")).status == 200
    finally:
        await spoke.close()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await server.stop()


async def test_shutdown_signal_runs_cleanup_and_removes_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    handlers: dict[Any, Any] = {}
    removed: list[Any] = []
    monkeypatch.setattr(
        loop, "add_signal_handler", lambda signum, callback: handlers.update({signum: callback})
    )
    monkeypatch.setattr(loop, "remove_signal_handler", lambda signum: removed.append(signum))
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def service() -> None:
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    task = asyncio.create_task(run_with_shutdown(service()))
    await started.wait()
    handlers[signal.SIGTERM]()
    await task
    assert cleaned.is_set()
    assert set(removed) == {signal.SIGTERM, signal.SIGINT}


async def test_shutdown_wrapper_preserves_startup_errors() -> None:
    async def broken() -> None:
        raise RuntimeError("synthetic-startup-failure")

    with pytest.raises(RuntimeError, match="synthetic-startup-failure"):
        await run_with_shutdown(broken())
