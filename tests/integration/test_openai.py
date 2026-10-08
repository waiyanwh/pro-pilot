from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from pro_pilot.hub.agent import AgentLoop
from pro_pilot.hub.llm import LLMUnavailable
from pro_pilot.hub.openai import OpenAIClient
from pro_pilot.hub.registry import SpokeRegistry
from pro_pilot.protocol.messages import Hello, ToolResult

API_KEY = "synthetic-test-api-credential"


@asynccontextmanager
async def model_api(
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> AsyncIterator[OpenAIClient]:
    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    async with TestServer(app) as server:
        client = OpenAIClient(
            base_url=str(server.make_url("/v1")),
            model_id="your-model-id",
            api_key=API_KEY,
            random_fn=lambda: 0,
        )
        try:
            yield client
        finally:
            await client.close()


def completion(content: str = "complete") -> dict[str, Any]:
    return {
        "choices": [
            {"finish_reason": "stop", "message": {"role": "assistant", "content": content}}
        ],
        "usage": {"prompt_tokens": 8, "completion_tokens": 6},
    }


class PodEvidence(SpokeRegistry):
    async def call(self, **request: Any) -> ToolResult:
        return ToolResult(id="wire-call", ok=True, data=[{"name": "api", "phase": "Running"}])


async def test_chat_round_trip_preserves_tool_correlation_metadata_and_usage() -> None:
    calls = 0
    signature = {"google": {"thought_signature": "synthetic-signature"}}

    async def handler(request: web.Request) -> web.Response:
        nonlocal calls
        calls += 1
        if request.headers.get("Authorization") != f"Bearer {API_KEY}":
            return web.json_response({}, status=401)
        if not request.headers.get("User-Agent", "").startswith("Mozilla/5.0"):
            return web.json_response({}, status=403)
        payload = await request.json()
        if calls == 1:
            return web.json_response(
                {
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "provider-call",
                                        "type": "function",
                                        "extra_content": signature,
                                        "function": {
                                            "name": "get_pods",
                                            "arguments": json.dumps(
                                                {"cluster": "test-cluster", "namespace": "payment"}
                                            ),
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2},
                }
            )
        previous = payload["messages"][-2]
        result = payload["messages"][-1]
        if (
            previous.get("tool_calls", [{}])[0].get("extra_content") != signature
            or result.get("role") != "tool"
            or result.get("tool_call_id") != "provider-call"
            or json.loads(result["content"]).get("data", [{}])[0].get("phase") != "Running"
        ):
            return web.json_response({}, status=400)
        return web.json_response(completion())

    registry = PodEvidence()
    await registry.register(
        Hello(cluster_id="test-cluster", spoke_version="0.1.0", tools=["get_pods"]), object()
    )
    async with model_api(handler) as client:
        result = await AgentLoop(client, registry).run("inspect api", cluster_ids=["test-cluster"])
    assert result.outcome == "answered"
    assert result.usage == {"inputTokens": 18, "outputTokens": 8}
    assert result.tools[0].result.ok
    assert calls == 2


@pytest.mark.parametrize("status,attempts", [(401, 1), (429, 3)])
async def test_http_failures_are_bounded_and_do_not_log_credentials(
    status: int, attempts: int, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls = 0
    real_sleep = asyncio.sleep

    async def no_delay(delay: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr("pro_pilot.hub.openai.asyncio.sleep", no_delay)

    async def handler(request: web.Request) -> web.Response:
        nonlocal calls
        calls += 1
        return web.json_response({"error": {"message": API_KEY}}, status=status)

    async with model_api(handler) as client:
        with pytest.raises(LLMUnavailable):
            await client.converse(system="test", messages=[], tool_names=[], cluster_ids=[])
    assert calls == attempts
    assert API_KEY not in caplog.text


@pytest.mark.parametrize(
    "payload",
    [
        {"choices": []},
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "bad",
                                "type": "function",
                                "function": {"name": "get_pods", "arguments": "invalid-json"},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
        {
            "choices": [
                {"message": {"role": "system", "content": "untrusted"}, "finish_reason": "stop"}
            ]
        },
    ],
)
async def test_malformed_provider_reply_fails_without_executing_tools(payload: Any) -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.json_response(payload)

    async with model_api(handler) as client:
        result = await AgentLoop(client, SpokeRegistry()).run("inspect api", cluster_ids=[])
    assert result.outcome == "llm_error"
    assert result.tools == []


async def test_final_generation_disables_tools_and_reports_token_limit() -> None:
    async def handler(request: web.Request) -> web.Response:
        payload = await request.json()
        if "tools" in payload or "tool_choice" in payload:
            return web.json_response({}, status=400)
        response = completion("partial evidence")
        response["choices"][0]["finish_reason"] = "length"
        return web.json_response(response)

    async with model_api(handler) as client:
        result = await client.converse(
            system="test",
            messages=[],
            tool_names=["get_pods"],
            cluster_ids=["test-cluster"],
            use_tools=False,
        )
    assert result["stopReason"] == "max_tokens"


async def test_redirect_is_not_followed_with_authorization() -> None:
    forwarded = 0
    app = web.Application()

    async def redirect(request: web.Request) -> web.Response:
        raise web.HTTPFound("/collector")

    async def collector(request: web.Request) -> web.Response:
        nonlocal forwarded
        forwarded += 1
        return web.json_response(completion())

    app.router.add_post("/v1/chat/completions", redirect)
    app.router.add_route("*", "/collector", collector)
    async with TestServer(app) as server:
        client = OpenAIClient(
            base_url=str(server.make_url("/v1")), model_id="your-model-id", api_key=API_KEY
        )
        try:
            with pytest.raises(LLMUnavailable):
                await client.converse(system="test", messages=[], tool_names=[], cluster_ids=[])
        finally:
            await client.close()
    assert forwarded == 0


async def test_oversized_response_is_rejected_instead_of_returning_partial_data() -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.json_response(completion("x" * (1024 * 1024)))

    async with model_api(handler) as client:
        with pytest.raises(LLMUnavailable):
            await client.converse(system="test", messages=[], tool_names=[], cluster_ids=[])
