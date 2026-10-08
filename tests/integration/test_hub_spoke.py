from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
import websockets
from websockets.exceptions import ConnectionClosed

from pro_pilot.hub.registry import RegistryError, SpokeRegistry
from pro_pilot.hub.spoke_server import SpokeServer
from pro_pilot.protocol.messages import (
    CloseCode,
    Hello,
    ToolCall,
    Welcome,
    encode_message,
    parse_message,
)
from pro_pilot.spoke.client import SpokeClient
from pro_pilot.spoke.fake import FakeFixtureExecutor
from pro_pilot.spoke.pipeline import ToolPipeline
from pro_pilot.spoke.policy import NamespacePolicy

TOKEN = "phase-zero-test-token"
CLUSTER = "demo-cluster"


@asynccontextmanager
async def running_server() -> AsyncIterator[tuple[SpokeServer, str]]:
    registry = SpokeRegistry({CLUSTER: [f"sha256:{SpokeRegistry.hash_token(TOKEN)}"]})
    server = SpokeServer(registry, host="127.0.0.1", port=0)
    await server.start()
    port = server._server.sockets[0].getsockname()[1]
    try:
        yield server, f"ws://127.0.0.1:{port}/spoke/v1/connect"
    finally:
        await server.stop()


def hello(cluster_id: str = CLUSTER) -> Hello:
    return Hello(
        cluster_id=cluster_id,
        spoke_version="0.1.0",
        capabilities=["k8s"],
        tools=["get_pods"],
    )


async def connect_spoke(url: str, *, cluster_id: str = CLUSTER, token: str = TOKEN) -> Any:
    websocket = await websockets.connect(
        url,
        additional_headers={
            "Authorization": f"Bearer {token}",
            "X-Pro-Pilot-Cluster": cluster_id,
        },
    )
    return websocket


@pytest.mark.asyncio
async def test_wrong_token_closes_with_4401() -> None:
    async with running_server() as (_, url):
        websocket = await connect_spoke(url, token="wrong")
        with pytest.raises(ConnectionClosed) as exc_info:
            await websocket.recv()
        assert exc_info.value.rcvd is not None
        assert exc_info.value.rcvd.code == CloseCode.AUTH_FAILED


@pytest.mark.asyncio
async def test_hello_cluster_mismatch_closes_with_4400() -> None:
    async with running_server() as (_, url):
        websocket = await connect_spoke(url)
        await websocket.send(encode_message(hello("different-cluster")))
        with pytest.raises(ConnectionClosed) as exc_info:
            await websocket.recv()
        assert exc_info.value.rcvd is not None
        assert exc_info.value.rcvd.code == CloseCode.BAD_HELLO


@pytest.mark.asyncio
async def test_new_connection_replaces_old_with_4409() -> None:
    async with running_server() as (_, url):
        old = await connect_spoke(url)
        await old.send(encode_message(hello()))
        assert isinstance(parse_message(await old.recv()), Welcome)
        new = await connect_spoke(url)
        await new.send(encode_message(hello()))
        assert isinstance(parse_message(await new.recv()), Welcome)
        with pytest.raises(ConnectionClosed) as exc_info:
            await old.recv()
        assert exc_info.value.rcvd is not None
        assert exc_info.value.rcvd.code == CloseCode.REPLACED
        await new.close()


@pytest.mark.asyncio
async def test_disconnect_mid_call_becomes_cluster_unavailable() -> None:
    async with running_server() as (server, url):
        websocket = await connect_spoke(url)
        await websocket.send(encode_message(hello()))
        assert isinstance(parse_message(await websocket.recv()), Welcome)
        connection = server.registry.get(CLUSTER)
        assert connection is not None
        call = asyncio.create_task(
            server.registry.call(
                cluster_id=CLUSTER,
                tool="get_pods",
                args={"namespace": "payment"},
                trace_id="trace",
                deadline_ms=5000,
            )
        )
        incoming = parse_message(await websocket.recv())
        assert isinstance(incoming, ToolCall)
        await websocket.close()
        with pytest.raises(RegistryError) as exc_info:
            await call
        assert exc_info.value.code.value == "CLUSTER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_registry_call_deadline_is_enforced() -> None:
    async with running_server() as (server, url):
        websocket = await connect_spoke(url)
        await websocket.send(encode_message(hello()))
        assert isinstance(parse_message(await websocket.recv()), Welcome)
        call = asyncio.create_task(
            server.registry.call(
                cluster_id=CLUSTER,
                tool="get_pods",
                args={"namespace": "payment"},
                trace_id="trace",
                deadline_ms=50,
            )
        )
        incoming = parse_message(await websocket.recv())
        assert isinstance(incoming, ToolCall)
        with pytest.raises(RegistryError) as exc_info:
            await call
        assert exc_info.value.code.value == "TIMEOUT"
        await websocket.close()


@pytest.mark.asyncio
async def test_real_fake_spoke_pipeline_round_trip() -> None:
    async with running_server() as (server, url):
        hello_message = hello()
        client = SpokeClient(
            url=url,
            token=TOKEN,
            hello=hello_message,
            pipeline=ToolPipeline(
                FakeFixtureExecutor(
                    "tests/fixtures/clusters/demo-cluster",
                    "crashloop-oom",
                ),
                NamespacePolicy(("*",), ()),
                advertised_tools={"get_pods"},
            ),
        )
        client_task = asyncio.create_task(client.run_once())
        for _ in range(50):
            if server.registry.get(CLUSTER) is not None:
                break
            await asyncio.sleep(0.01)
        result = await server.registry.call(
            cluster_id=CLUSTER,
            tool="get_pods",
            args={"namespace": "payment"},
            trace_id="trace",
            deadline_ms=1000,
        )
        assert result.ok
        assert isinstance(result.data, list)
        assert result.data[0]["last_termination"]["reason"] == "OOMKilled"
        await client.close()
        await client_task


class AnswerBedrock:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def converse(self, **request: Any) -> dict[str, Any]:
        self.calls.append(request)
        if len(self.calls) == 1:
            return {
                "stopReason": "tool_use",
                "output": {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "toolUse": {
                                    "toolUseId": "answer-call",
                                    "name": "get_pods",
                                    "input": {"cluster": CLUSTER, "namespace": "payment"},
                                }
                            }
                        ],
                    }
                },
            }
        return {
            "stopReason": "end_turn",
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "text": (
                                ":red_circle: payment-api is crash-looping because it was "
                                "OOMKilled."
                            )
                        }
                    ],
                }
            },
        }


@pytest.mark.asyncio
async def test_agent_answer_loop_uses_fixture_evidence_over_real_websocket() -> None:
    async with running_server() as (server, url):
        client = SpokeClient(
            url=url,
            token=TOKEN,
            hello=hello(),
            pipeline=ToolPipeline(
                FakeFixtureExecutor(
                    "tests/fixtures/clusters/demo-cluster",
                    "crashloop-oom",
                ),
                NamespacePolicy(("*",), ()),
                advertised_tools={"get_pods"},
            ),
        )
        client_task = asyncio.create_task(client.run_once())
        for _ in range(50):
            if server.registry.get(CLUSTER) is not None:
                break
            await asyncio.sleep(0.01)
        from pro_pilot.hub.agent import AgentConfig, AgentLoop

        bedrock = AnswerBedrock()
        result = await AgentLoop(
            bedrock,
            server.registry,
            config=AgentConfig(turn_deadline_s=2),
        ).run(
            "why is payment-api crashlooping?",
            cluster_ids=[CLUSTER],
        )
        assert "OOMKilled" in result.text
        assert result.tools[0].result.ok
        assert len(bedrock.calls) == 2
        await client.close()
        await client_task


@pytest.mark.parametrize(
    ("frame", "code"),
    [
        (b"{}", CloseCode.BAD_HELLO),
        ('{"type":[],"v":1}', CloseCode.BAD_HELLO),
        ('{"type":"hello","v":99}', CloseCode.PROTOCOL_UNSUPPORTED),
    ],
)
async def test_malformed_handshake_closes_with_protocol_code(frame: str | bytes, code: int) -> None:
    async with running_server() as (_, url), await connect_spoke(url) as websocket:
        await websocket.send(frame)
        with pytest.raises(ConnectionClosed) as caught:
            await websocket.recv()
        assert caught.value.rcvd is not None
        assert caught.value.rcvd.code == code


async def test_unknown_advertised_tool_is_not_routable() -> None:
    async with running_server() as (server, url), await connect_spoke(url) as websocket:
        await websocket.send(
            encode_message(hello().model_copy(update={"tools": ["get_pods", "future_tool"]}))
        )
        assert isinstance(parse_message(await websocket.recv()), Welcome)
        with pytest.raises(RegistryError) as caught:
            await server.registry.call(
                cluster_id=CLUSTER,
                tool="future_tool",
                args={},
                trace_id="trace",
                deadline_ms=100,
            )
        assert caught.value.code.value == "TOOL_NOT_SUPPORTED"


async def test_replacement_fails_old_pending_call_but_new_connection_works() -> None:
    async with (
        running_server() as (server, url),
        await connect_spoke(url) as old,
        await connect_spoke(url) as new,
    ):
        await old.send(encode_message(hello()))
        await old.recv()
        request = asyncio.create_task(
            server.registry.call(
                cluster_id=CLUSTER,
                tool="get_pods",
                args={"namespace": "payment"},
                trace_id="trace",
                deadline_ms=1000,
            )
        )
        assert isinstance(parse_message(await old.recv()), ToolCall)
        await new.send(encode_message(hello()))
        assert isinstance(parse_message(await new.recv()), Welcome)
        with pytest.raises(RegistryError) as caught:
            await request
        assert caught.value.code.value == "CLUSTER_UNAVAILABLE"
        next_request = asyncio.create_task(
            server.registry.call(
                cluster_id=CLUSTER,
                tool="get_pods",
                args={"namespace": "payment"},
                trace_id="trace",
                deadline_ms=1000,
            )
        )
        incoming = parse_message(await new.recv())
        assert isinstance(incoming, ToolCall)
        from pro_pilot.protocol.messages import ToolResult

        await new.send(encode_message(ToolResult(id=incoming.id, ok=True, data=[])))
        assert (await next_request).ok


class ReconnectRecorder(SpokeClient):
    def __init__(self, url: str) -> None:
        super().__init__(
            url=url,
            token=TOKEN,
            hello=hello(),
            random_fn=lambda: 0.5,
            pipeline=ToolPipeline(
                FakeFixtureExecutor("tests/fixtures/clusters/demo-cluster", "crashloop-oom"),
                NamespacePolicy(("*",), ()),
            ),
        )
        self.delays: list[float] = []

    async def _sleep_or_stop(self, delay: float) -> None:
        self.delays.append(delay)
        self._stop.set()


@pytest.mark.parametrize(
    ("close_code", "delay"),
    [(CloseCode.NORMAL, 1), (CloseCode.AUTH_FAILED, 300), (CloseCode.REPLACED, 30)],
)
async def test_spoke_close_codes_schedule_required_reconnect(close_code: int, delay: float) -> None:
    async def close_connection(websocket: Any) -> None:
        await websocket.recv()
        if close_code != CloseCode.AUTH_FAILED:
            await websocket.send(encode_message(Welcome(session_id="test-session")))
        await websocket.close(code=close_code)

    async with websockets.serve(close_connection, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = ReconnectRecorder(f"ws://127.0.0.1:{port}/spoke/v1/connect")
        await asyncio.wait_for(client.run_forever(), timeout=2)
        assert client.delays == [delay]
        assert client._websocket is None


async def test_unsupported_protocol_stops_spoke_without_reconnecting() -> None:
    from pro_pilot.spoke.client import ProtocolUnsupported

    async def reject_protocol(websocket: Any) -> None:
        await websocket.recv()
        await websocket.close(code=CloseCode.PROTOCOL_UNSUPPORTED)

    async with websockets.serve(reject_protocol, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = ReconnectRecorder(f"ws://127.0.0.1:{port}/spoke/v1/connect")
        with pytest.raises(ProtocolUnsupported):
            await asyncio.wait_for(client.run_forever(), timeout=2)
        assert client.delays == []
        assert client._websocket is None
