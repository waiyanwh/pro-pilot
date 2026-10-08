from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

import pytest

from pro_pilot.hub.agent import AgentConfig, AgentLoop
from pro_pilot.hub.registry import SpokeRegistry
from pro_pilot.hub.slack_app import SlackConfig, SlackHandler
from pro_pilot.hub.thread_store import ThreadStore
from pro_pilot.protocol.messages import Hello, ToolResult


def conversation_turn(question: str, call_id: str | None = None) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "user", "content": [{"text": question}]}]
    if call_id:
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "toolUse": {
                                "toolUseId": call_id,
                                "name": "get_pods",
                                "input": {"namespace": "payment"},
                            }
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "toolResult": {
                                "toolUseId": call_id,
                                "status": "success",
                                "content": [{"json": []}],
                            }
                        }
                    ],
                },
            ]
        )
    messages.append({"role": "assistant", "content": [{"text": "complete"}]})
    return messages


def test_thread_limit_bounds_turns_without_tool_use() -> None:
    store = ThreadStore(max_turns=1)
    store.put("C", "T", conversation_turn("first") + conversation_turn("second"))
    assert store.get("C", "T") == conversation_turn("second")


def test_thread_limit_preserves_matched_tool_use_and_result() -> None:
    store = ThreadStore(max_turns=1)
    newest = conversation_turn("second", "second-call")
    store.put("C", "T", conversation_turn("first", "first-call") + newest)
    assert store.get("C", "T") == newest


class EvidenceModel:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def converse(self, **request: Any) -> dict[str, Any]:
        self.requests.append(copy.deepcopy(request))
        if len(self.requests) == 1:
            return {
                "stopReason": "tool_use",
                "output": {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "toolUse": {
                                    "toolUseId": "call",
                                    "name": "get_pods",
                                    "input": {"cluster": "test-cluster", "namespace": "payment"},
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
                    "content": [{"text": "complete"}],
                }
            },
        }


class LocalRegistry(SpokeRegistry):
    def __init__(self, data: Any) -> None:
        super().__init__()
        self.data = data
        self.deadline_ms = 0

    async def call(self, **request: Any) -> ToolResult:
        self.deadline_ms = request["deadline_ms"]
        return ToolResult(id="wire-call", ok=True, data=self.data, truncated=True)


async def test_tool_deadline_and_escaped_evidence_are_bounded() -> None:
    registry = LocalRegistry([{"message": '"\\\n' * 3000}])
    await registry.register(
        Hello(cluster_id="test-cluster", spoke_version="0.1.0", tools=["get_pods"]), object()
    )
    model = EvidenceModel()
    result = await AgentLoop(
        model, registry, config=AgentConfig(turn_deadline_s=1, tool_result_chars_to_llm=500)
    ).run("inspect pods", cluster_ids=["test-cluster"])
    assert result.outcome == "answered"
    assert 0 < registry.deadline_ms <= 1000
    content = model.requests[1]["messages"][-1]["content"][0]["toolResult"]["content"][0]
    assert len(json.dumps(content, ensure_ascii=False)) <= 500
    assert content["json"]["truncated"] is True
    assert content["json"]["cluster"] == "test-cluster"


class InterruptedModel(EvidenceModel):
    async def converse(self, **request: Any) -> dict[str, Any]:
        if self.requests:
            await asyncio.Event().wait()
        return await super().converse(**request)


async def test_deadline_failure_keeps_completed_evidence_but_not_orphan_history() -> None:
    registry = LocalRegistry([])
    await registry.register(
        Hello(cluster_id="test-cluster", spoke_version="0.1.0", tools=["get_pods"]), object()
    )
    result = await AgentLoop(
        InterruptedModel(), registry, config=AgentConfig(turn_deadline_s=0.05)
    ).run("inspect pods", cluster_ids=["test-cluster"])
    assert result.outcome == "deadline"
    assert result.tools[0].result.ok
    assert [message["role"] for message in result.messages] == ["user", "assistant"]
    assert all(
        "toolUse" not in block for message in result.messages for block in message["content"]
    )


class RejectionSlack:
    async def chat_postEphemeral(self, **params: Any) -> None:
        pass


class UncalledAgent:
    async def run(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Rejected questions must not reach the model")


@pytest.mark.parametrize("outcome", ["refused", "rate_limited"])
async def test_rejected_question_has_secret_safe_audit_without_execution(
    outcome: str, caplog: pytest.LogCaptureFixture
) -> None:
    handler = SlackHandler(
        RejectionSlack(), UncalledAgent(), SlackConfig(channels={"C-allowed": ["test-cluster"]})
    )
    if outcome == "rate_limited":
        assert handler.rate_limiter.acquire("U-test")[0]
    with caplog.at_level("INFO", logger="pro_pilot.audit"):
        await handler.handle_app_mention(
            {
                "channel": "C-allowed" if outcome == "rate_limited" else "C-denied",
                "user": "U-test",
                "ts": "1.0",
                "text": "<@B-test> password=private-value inspect pods",
            }
        )
    record = json.loads(caplog.records[-1].getMessage())
    assert record["outcome"] == outcome
    assert record["tool_calls"] == [] and record["clusters"] == []
    assert record["llm"]["input_tokens"] == 0 and record["llm"]["output_tokens"] == 0
    assert "private-value" not in record["question"]
