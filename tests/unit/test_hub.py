from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, cast

import pytest

from pro_pilot.hub.agent import AgentConfig, AgentLoop, AgentResult, ToolRecord
from pro_pilot.hub.audit import AuditLog
from pro_pilot.hub.llm import LLMUnavailable
from pro_pilot.hub.policy import RateLimitConfig, RateLimiter
from pro_pilot.hub.registry import SpokeRegistry
from pro_pilot.hub.slack_app import SlackConfig, SlackHandler
from pro_pilot.hub.slack_format import build_answer_payload, normalize_slack_mrkdwn
from pro_pilot.hub.thread_store import ThreadStore
from pro_pilot.protocol.messages import ToolResult


class StubBedrock:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def converse(self, **request: Any) -> dict[str, Any]:
        self.calls.append(request)
        return self.responses.pop(0)


@dataclass
class FakeSlackClient:
    posts: list[dict[str, Any]]
    updates: list[dict[str, Any]]
    ephemeral: list[dict[str, Any]]

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.posts.append(kwargs)
        return {"ts": "placeholder-ts"}

    async def chat_update(self, **kwargs: Any) -> dict[str, Any]:
        self.updates.append(kwargs)
        return kwargs

    async def chat_postEphemeral(self, **kwargs: Any) -> dict[str, Any]:
        self.ephemeral.append(kwargs)
        return kwargs


class FakeAgent:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run(self, question: str, **kwargs: Any) -> AgentResult:
        self.calls.append(question)
        return AgentResult(text=":white_check_mark: done", trace_id="trace")


def _tool_response(*names: str) -> dict[str, Any]:
    return {
        "stopReason": "tool_use",
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": f"id-{name}",
                            "name": name,
                            "input": {"cluster": "staging", "namespace": "payment"},
                        }
                    }
                    for name in names
                ],
            }
        },
    }


@pytest.mark.asyncio
async def test_agent_stops_at_iteration_limit_with_final_no_tools_call() -> None:
    bedrock = StubBedrock(
        [
            _tool_response("get_pods"),
            _tool_response("get_pods"),
            {"stopReason": "end_turn", "output": {"message": {"content": [{"text": "done"}]}}},
        ]
    )
    agent = AgentLoop(
        bedrock, SpokeRegistry(), config=AgentConfig(max_iterations=2, turn_deadline_s=1)
    )
    result = await agent.run("question", cluster_ids=["staging"])
    assert result.iterations == 2
    assert len(bedrock.calls) == 3
    assert result.text == "done"


@pytest.mark.asyncio
async def test_agent_rejects_cluster_not_mapped_without_registry_contact() -> None:
    bedrock = StubBedrock(
        [
            _tool_response("get_pods"),
            {"stopReason": "end_turn", "output": {"message": {"content": [{"text": "refused"}]}}},
        ]
    )
    agent = AgentLoop(bedrock, SpokeRegistry(), config=AgentConfig(turn_deadline_s=1))
    result = await agent.run("question", cluster_ids=["allowed"])
    assert result.tools[0].result.error is not None
    assert result.tools[0].result.error.code.value == "CLUSTER_NOT_ALLOWED"


@pytest.mark.asyncio
async def test_agent_tool_budget_is_reported_to_model() -> None:
    bedrock = StubBedrock(
        [
            _tool_response("get_pods", "get_events"),
            {"stopReason": "end_turn", "output": {"message": {"content": [{"text": "done"}]}}},
        ]
    )
    agent = AgentLoop(
        bedrock,
        SpokeRegistry(),
        config=AgentConfig(max_tool_calls=1, turn_deadline_s=1),
    )
    result = await agent.run("question", cluster_ids=["allowed"])
    assert any(
        block.get("toolResult", {})
        .get("content", [{}])[0]
        .get("json", {})
        .get("error", {})
        .get("code")
        == "TOOL_BUDGET_EXCEEDED"
        for message in result.messages
        for block in message.get("content", [])
    )


def test_rate_limiter_enforces_user_and_global_limits() -> None:
    limiter = RateLimiter(
        per_user=RateLimitConfig(2, 600),
        global_limit=RateLimitConfig(3, 600),
    )
    assert limiter.acquire("u1")[0]
    limiter.release("u1")
    assert limiter.acquire("u1")[0]
    limiter.release("u1")
    assert not limiter.acquire("u1")[0]
    assert limiter.acquire("u2")[0]
    limiter.release("u2")
    assert not limiter.acquire("u3")[0]


@pytest.mark.asyncio
async def test_slack_non_allowlisted_channel_never_calls_agent() -> None:
    client = FakeSlackClient([], [], [])
    agent = FakeAgent()
    handler = SlackHandler(
        client,
        agent,
        config=SlackConfig(channels={"C-allowed": ["staging"]}),
    )
    await handler.handle_app_mention(
        {"channel": "C-denied", "user": "U1", "text": "<@BOT> question", "ts": "1"}
    )
    assert agent.calls == []
    assert client.ephemeral[0]["unfurl_links"] is False
    assert client.ephemeral[0]["unfurl_media"] is False


def test_build_answer_payload_splits_block_kit_sections() -> None:
    answer = (
        ":red_circle: payment-api is crash-looping due to *OOMKilled*.\n\n"
        "*Evidence*\n"
        "• `payment/pod/payment-api-0`: CrashLoopBackOff, 14 restarts\n"
        "• Previous logs: `OutOfMemoryError`\n\n"
        "*Next steps*\n"
        "• Ask your platform team to review memory limits"
    )
    fallback, blocks_raw = build_answer_payload(
        answer, "Staging · 👍 Helpful · 👎 Needs improvement"
    )
    blocks = cast(list[dict[str, Any]], blocks_raw)
    types = [str(block["type"]) for block in blocks]
    assert types[0] == "header"
    assert types.count("section") == 2
    assert types[-2] == "divider"
    assert types[-1] == "context"
    section_texts = [
        str(block["text"]["text"]) for block in blocks if block.get("type") == "section"
    ]
    assert "CrashLoopBackOff" in section_texts[0]
    assert "What I found" in section_texts[0]
    assert "What to do next" in section_texts[1]
    assert "\n\n*What I found*\n" in fallback
    assert "\n\nStaging · 👍 Helpful" in fallback


@pytest.mark.parametrize(
    ("answer", "headline", "body"),
    [
        (
            ":white_check_mark: *Both application instances are ready*\n\n"
            "Both instances in `argo-rollout-poc` are running, with no restarts reported.",
            "✅ Both application instances are ready",
            "Both instances in `argo-rollout-poc` are running, with no restarts reported.",
        ),
        (
            ":white_check_mark: *No unavailable servers found*\n"
            "No servers were reported as NotReady in this check.",
            "✅ No unavailable servers found",
            "No servers were reported as NotReady in this check.",
        ),
        (
            ":information_source: *This area is outside my access*\n"
            "I can't check `kube-system`. Ask your platform team if you need a check there.",
            "ℹ️ This area is outside my access",
            "I can't check `kube-system`. Ask your platform team if you need a check there.",
        ),
        (
            ":information_source: *I can check status, but I can't restart applications*\n"
            "Ask the service owner or your platform team to make the change.",
            "ℹ️ I can check status, but I can't restart applications",
            "Ask the service owner or your platform team to make the change.",
        ),
    ],
)
def test_compact_answers_have_plain_text_headlines_and_separate_feedback(
    answer: str, headline: str, body: str
) -> None:
    footer = "Pro Infra Staging · 👍 Helpful · 👎 Needs improvement"
    fallback, raw = build_answer_payload(answer, footer)
    blocks = cast(list[dict[str, Any]], raw)
    assert [block["type"] for block in blocks] == ["header", "section", "divider", "context"]
    assert blocks[0]["text"] == {"type": "plain_text", "text": headline, "emoji": True}
    assert blocks[1]["text"]["text"] == body
    assert blocks[-1]["elements"][0]["text"] == footer
    assert "\n\n" + footer in fallback
    assert "What I found" not in fallback


def test_friendly_findings_and_actions_keep_exact_identifiers() -> None:
    answer = (
        "🔴 *payment-api keeps restarting*\nIt exceeded its memory limit.\n\n"
        "*What I found*\n• `payment-api-7d9c-abcde`: 14 restarts\n\n"
        "*What to do next*\n• Ask the service owner to review memory usage"
    )
    fallback, raw = build_answer_payload(answer, "👍 Helpful · 👎 Needs improvement")
    blocks = cast(list[dict[str, Any]], raw)
    sections = [block["text"]["text"] for block in blocks if block["type"] == "section"]
    assert sections == [
        "It exceeded its memory limit.",
        "*What I found*\n• `payment-api-7d9c-abcde`: 14 restarts",
        "*What to do next*\n• Ask the service owner to review memory usage",
    ]
    assert "payment-api-7d9c-abcde" in fallback


def test_oversized_headline_preserves_text_in_bounded_sections() -> None:
    answer = ":grey_question: " + "resource-name" * 300
    fallback, raw = build_answer_payload(answer, "👍 Helpful · 👎 Needs improvement")
    blocks = cast(list[dict[str, Any]], raw)
    sections = [block for block in blocks if block["type"] == "section"]
    assert not any(block["type"] == "header" for block in blocks)
    assert all(0 < len(block["text"]["text"]) <= 3000 for block in sections)
    assert "".join(block["text"]["text"] for block in sections).replace(" ", "") == answer.replace(
        " ", ""
    )
    assert answer in fallback


def test_requested_code_blocks_keep_line_breaks_and_do_not_parse_data_as_headings() -> None:
    logs = (
        "```\nINFO worker healthy\n*What to do next*\n"
        "## literal log heading\n**literal log data**\n  indented log data\n```"
    )
    answer = (
        ":information_source: *Requested log details*\n"
        "These are the returned lines.\n\n"
        f"*What I found*\n• Logs:\n{logs}\n\n"
        "*What to do next*\n• Ask the service owner to review these lines"
    )
    fallback, raw = build_answer_payload(answer, "👍 Helpful · 👎 Needs improvement")
    blocks = cast(list[dict[str, Any]], raw)
    sections = [block["text"]["text"] for block in blocks if block["type"] == "section"]
    assert sections[1] == "*What I found*\n• Logs:\n" + logs
    assert sections[2] == "*What to do next*\n• Ask the service owner to review these lines"
    assert logs in fallback


def test_markdown_normalization_preserves_requested_literal_code() -> None:
    text = "## **Details**\n`**literal**`\n```\n# raw log\n__raw data__\n```"
    assert normalize_slack_mrkdwn(text) == (
        "*Details*\n`**literal**`\n```\n# raw log\n__raw data__\n```"
    )


def test_code_only_answer_does_not_create_an_empty_slack_header() -> None:
    answer = "```\nINFO worker healthy\n```"
    fallback, raw = build_answer_payload(answer, "👍 Helpful · 👎 Needs improvement")
    blocks = cast(list[dict[str, Any]], raw)
    assert [block["type"] for block in blocks] == ["section", "divider", "context"]
    assert blocks[0]["text"]["text"] == answer
    assert answer in fallback


async def test_friendly_footer_keeps_tool_names_and_timing_in_audit_only() -> None:
    class CheckedAgent:
        async def run(self, question: str, **_: Any) -> AgentResult:
            return AgentResult(
                text=":white_check_mark: *Both instances are ready*\nNo restarts were reported.",
                trace_id="checked-trace",
                tools=[
                    ToolRecord(
                        "get_pods",
                        "demo-cluster",
                        {"namespace": "argo-rollout-poc"},
                        ToolResult(id="pods", ok=True, data=[], duration_ms=42),
                    )
                ],
            )

    records: list[dict[str, Any]] = []

    class CapturingLogger:
        def info(self, msg: str) -> None:
            records.append(json.loads(msg))

    client = FakeSlackClient([], [], [])
    handler = SlackHandler(
        client,
        CheckedAgent(),
        config=SlackConfig(channels={"C": ["demo-cluster"]}),
        audit=AuditLog(cast(Any, CapturingLogger())),
    )
    await handler.handle_app_mention(
        {"channel": "C", "user": "U", "text": "<@BOT> status?", "ts": "1"}
    )
    message = client.updates[0]
    footer = message["blocks"][-1]["elements"][0]["text"]
    assert footer == "Pro Infra Staging · 👍 Helpful · 👎 Needs improvement"
    assert "get_pods" not in message["text"]
    assert "no cluster queried" not in message["text"]
    assert "`" not in footer
    assert message["unfurl_links"] is False and message["unfurl_media"] is False
    record = next(row for row in records if row["event"] == "audit.question")
    assert record["tool_calls"][0]["tool"] == "get_pods"
    assert record["tool_calls"][0]["duration_ms"] == 42
    assert "duration_ms" in record


@pytest.mark.asyncio
async def test_slack_allowed_channel_posts_and_updates_without_unfurls() -> None:
    client = FakeSlackClient([], [], [])
    agent = FakeAgent()
    handler = SlackHandler(
        client,
        agent,
        config=SlackConfig(channels={"C-allowed": ["staging"]}),
    )
    await handler.handle_app_mention(
        {"channel": "C-allowed", "user": "U1", "text": "<@BOT> why?", "ts": "1"}
    )
    assert agent.calls == ["why?"]
    assert client.posts[0]["unfurl_links"] is False
    assert client.posts[0]["unfurl_media"] is False
    assert client.updates[0]["unfurl_links"] is False
    assert client.updates[0]["unfurl_media"] is False
    assert client.updates[0]["blocks"][-1]["type"] == "context"
    assert client.updates[0]["blocks"][-1]["elements"][0]["text"] == (
        "👍 Helpful · 👎 Needs improvement"
    )
    assert "no cluster queried" not in client.updates[0]["text"]


@pytest.mark.asyncio
async def test_agent_sums_token_usage_across_converse_calls() -> None:
    first = _tool_response("get_pods")
    first["usage"] = {"inputTokens": 100, "outputTokens": 20}
    second = {
        "stopReason": "end_turn",
        "output": {"message": {"content": [{"text": "done"}]}},
        "usage": {"inputTokens": 150, "outputTokens": 30},
    }
    bedrock = StubBedrock([first, second])
    agent = AgentLoop(bedrock, SpokeRegistry(), config=AgentConfig(turn_deadline_s=1))
    result = await agent.run("question", cluster_ids=["allowed"])
    assert result.usage == {"inputTokens": 250, "outputTokens": 50}


@pytest.mark.asyncio
async def test_agent_retains_tool_progress_when_bedrock_fails_mid_loop() -> None:
    class FlakyBedrock:
        def __init__(self) -> None:
            self.calls = 0

        async def converse(self, **_: Any) -> dict[str, Any]:
            self.calls += 1
            if self.calls == 1:
                return _tool_response("get_pods")
            raise LLMUnavailable("boom")

    agent = AgentLoop(FlakyBedrock(), SpokeRegistry(), config=AgentConfig(turn_deadline_s=1))
    result = await agent.run("question", cluster_ids=["allowed"])
    assert result.outcome == "llm_error"
    assert result.iterations == 1
    assert len(result.tools) == 1
    assert result.tools[0].tool == "get_pods"


@pytest.mark.asyncio
async def test_agent_retains_tool_progress_on_deadline() -> None:
    class SlowBedrock:
        def __init__(self) -> None:
            self.calls = 0

        async def converse(self, **_: Any) -> dict[str, Any]:
            self.calls += 1
            if self.calls == 1:
                return _tool_response("get_pods")
            await asyncio.sleep(1)
            return {
                "stopReason": "end_turn",
                "output": {"message": {"content": [{"text": "late"}]}},
            }

    agent = AgentLoop(SlowBedrock(), SpokeRegistry(), config=AgentConfig(turn_deadline_s=0.05))
    result = await agent.run("question", cluster_ids=["allowed"])
    assert result.outcome == "deadline"
    assert result.iterations == 1
    assert len(result.tools) == 1


def test_rate_limiter_global_rejection_does_not_consume_user_quota() -> None:
    limiter = RateLimiter(
        per_user=RateLimitConfig(1, 600),
        global_limit=RateLimitConfig(1, 600),
    )
    assert limiter.acquire("u1")[0]
    limiter.release("u1")
    # Global window is now full; u2's attempt is rejected on the global check.
    assert not limiter.acquire("u2")[0]
    # Simulate the global window clearing (time passing). If the earlier rejection had
    # already burned u2's own per-user quota slot, this would incorrectly fail too.
    limiter.global_window._timestamps.clear()  # type: ignore[attr-defined]
    assert limiter.acquire("u2")[0]


def test_thread_store_trims_tool_result_content_but_keeps_valid_shape() -> None:
    store = ThreadStore(max_turns=20)
    oversized = {"json": {"ok": True, "data": {"lines": ["x" * 3000]}}}
    messages = [
        {"role": "user", "content": [{"text": "q"}]},
        {
            "role": "assistant",
            "content": [{"toolUse": {"toolUseId": "1", "name": "get_pod_logs", "input": {}}}],
        },
        {
            "role": "user",
            "content": [
                {"toolResult": {"toolUseId": "1", "status": "success", "content": [oversized]}}
            ],
        },
    ]
    store.put("C1", "T1", messages)
    restored = store.get("C1", "T1")
    tool_result = restored[2]["content"][0]["toolResult"]
    assert tool_result["toolUseId"] == "1"
    assert tool_result["status"] == "success"
    assert len(tool_result["content"]) == 1
    assert "text" in tool_result["content"][0]


def test_audit_question_redacts_tool_call_argument_values() -> None:
    records: list[str] = []

    class CapturingLogger:
        def info(self, msg: str) -> None:
            records.append(msg)

    audit = AuditLog(cast(Any, CapturingLogger()))
    audit.question(
        trace_id="t1",
        slack_user="U1",
        channel="C1",
        thread_ts="1",
        question="how do I use password=supersecret123?",
        clusters=["staging"],
        tool_calls=[
            {
                "tool": "get_pods",
                "cluster": "staging",
                "args": {"token": "xoxb-aaaa-bbbb-cccccccccccccccccccccccc"},
                "ok": True,
                "error_code": None,
                "duration_ms": 1,
                "bytes": 0,
                "redactions": 0,
            }
        ],
        llm={},
        outcome="answered",
        duration_ms=1,
    )
    logged = records[0]
    assert "supersecret123" not in logged
    assert "xoxb-aaaa-bbbb" not in logged


@pytest.mark.asyncio
async def test_slack_feedback_ties_reaction_to_pro_pilot_trace_id() -> None:
    class TracedAgent:
        async def run(self, question: str, **_: Any) -> AgentResult:
            return AgentResult(text="ok", trace_id="trace-xyz")

    records: list[dict[str, Any]] = []

    class CapturingLogger:
        def info(self, msg: str) -> None:
            records.append(json.loads(msg))

    handler = SlackHandler(
        FakeSlackClient([], [], []),
        TracedAgent(),
        config=SlackConfig(channels={"C-allowed": ["staging"]}),
        audit=AuditLog(cast(Any, CapturingLogger())),
    )
    await handler.handle_app_mention(
        {"channel": "C-allowed", "user": "U1", "text": "<@BOT> why?", "ts": "1"}
    )
    await handler.handle_reaction_added(
        {
            "reaction": "+1",
            "user": "U2",
            "item": {"type": "message", "channel": "C-allowed", "ts": "placeholder-ts"},
        }
    )
    feedback = [r for r in records if r["event"] == "audit.feedback"]
    assert feedback and feedback[0]["trace_id"] == "trace-xyz"


@pytest.mark.asyncio
async def test_slack_feedback_ignored_for_message_pro_pilot_never_answered() -> None:
    records: list[dict[str, Any]] = []

    class CapturingLogger:
        def info(self, msg: str) -> None:
            records.append(json.loads(msg))

    handler = SlackHandler(
        FakeSlackClient([], [], []),
        FakeAgent(),
        config=SlackConfig(channels={"C-allowed": ["staging"]}),
        audit=AuditLog(cast(Any, CapturingLogger())),
    )
    await handler.handle_reaction_added(
        {
            "reaction": "+1",
            "user": "U2",
            "item": {"type": "message", "channel": "C-allowed", "ts": "untracked-ts"},
        }
    )
    assert records == []
