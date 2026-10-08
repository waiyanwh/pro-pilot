"""Slack event handling with channel policy, limits, and safe posting defaults."""

from __future__ import annotations

import re
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from html import escape
from typing import Any

from .agent import AgentResult
from .audit import AuditLog, elapsed_ms
from .policy import RateLimiter, clusters_for_channel
from .slack_format import build_answer_payload, normalize_slack_mrkdwn
from .thread_store import ThreadStore


@dataclass(frozen=True)
class SlackConfig:
    channels: dict[str, list[str]]
    allow_dm: bool = False
    max_question_chars: int = 1000


_MAX_TRACE_ENTRIES = 2000


class SlackHandler:
    def __init__(
        self,
        client: Any,
        agent: Any,
        config: SlackConfig,
        rate_limiter: RateLimiter | None = None,
        thread_store: ThreadStore | None = None,
        audit: AuditLog | None = None,
    ) -> None:
        self.client = client
        self.agent = agent
        self.config = config
        self.rate_limiter = rate_limiter or RateLimiter()
        self.thread_store = thread_store or ThreadStore()
        self.audit = audit or AuditLog()
        # Bounded (channel, message_ts) -> trace_id map so a reaction on a pro-pilot
        # answer can be tied back to that answer's audit.question record. Bot messages
        # we never answered are not in this map and get no feedback record.
        self._trace_by_message: OrderedDict[tuple[str, str], str] = OrderedDict()

    async def handle_app_mention(self, event: dict[str, Any]) -> None:
        if event.get("bot_id") or event.get("subtype") in {"bot_message", "message_changed"}:
            return
        channel = str(event.get("channel", ""))
        user = str(event.get("user", ""))
        question = _strip_mention(str(event.get("text", ""))).strip()
        thread_ts = str(event.get("thread_ts") or event.get("ts", ""))
        started = time.monotonic()
        trace_id = uuid.uuid4().hex

        def audit_rejection(outcome: str) -> None:
            self.audit.question(
                trace_id=trace_id,
                slack_user=user,
                channel=channel,
                thread_ts=thread_ts,
                question=question,
                clusters=[],
                tool_calls=[],
                llm={"model_id": None, "input_tokens": 0, "output_tokens": 0, "iterations": 0},
                outcome=outcome,
                duration_ms=elapsed_ms(started),
            )

        if channel not in self.config.channels:
            await self._ephemeral(
                channel,
                user,
                "pro-pilot isn't enabled in this channel. Ask your platform team to add it.",
            )
            audit_rejection("refused")
            return
        if not question:
            await self._post(
                channel,
                (
                    "Ask me about an application's status or a deployment. For example:\n"
                    "• Is staging healthy?\n"
                    "• Why is payment-api restarting?\n"
                    "• Has identity finished deploying?"
                ),
                thread_ts=thread_ts,
            )
            audit_rejection("refused")
            return
        if len(question) > self.config.max_question_chars:
            await self._post(
                channel,
                f":warning: Questions are limited to {self.config.max_question_chars} characters.",
                thread_ts=thread_ts,
            )
            audit_rejection("refused")
            return
        allowed, retry_s = self.rate_limiter.acquire(user)
        if not allowed:
            await self._ephemeral(
                channel,
                user,
                (f"You've hit the question limit. Try again in {max(1, int(retry_s / 60))} min."),
            )
            audit_rejection("rate_limited")
            return
        placeholder: dict[str, Any] | None = None
        try:
            placeholder = await self._post(channel, ":mag: Looking into it…", thread_ts=thread_ts)
            result = await self.agent.run(
                question,
                cluster_ids=clusters_for_channel(channel, self.config.channels),
                history=self.thread_store.get(channel, thread_ts),
                trace_id=trace_id,
            )
            answer = _limit_answer(normalize_slack_mrkdwn(result.text))
            footer = _footer(result)
            message_ts = str(placeholder.get("ts", ""))
            await self._update(channel, message_ts, answer, footer)
            self._remember_trace(channel, message_ts, result.trace_id)
            self.thread_store.put(channel, thread_ts, result.messages)
            self.audit.question(
                trace_id=result.trace_id,
                slack_user=user,
                channel=channel,
                thread_ts=thread_ts,
                question=question,
                clusters=sorted({record.cluster for record in result.tools if record.cluster}),
                tool_calls=[
                    {
                        "tool": record.tool,
                        "cluster": record.cluster,
                        "args": record.args,
                        "ok": record.result.ok,
                        "error_code": record.result.error.code.value
                        if record.result.error
                        else None,
                        "duration_ms": record.result.duration_ms,
                        "bytes": len(str(record.result.data).encode())
                        if record.result.data is not None
                        else 0,
                        "redactions": record.result.redactions,
                    }
                    for record in result.tools
                ],
                llm={
                    "model_id": getattr(getattr(self.agent, "llm", None), "model_id", None),
                    "input_tokens": result.usage.get("inputTokens", 0),
                    "output_tokens": result.usage.get("outputTokens", 0),
                    "iterations": result.iterations,
                },
                outcome=result.outcome,
                duration_ms=elapsed_ms(started),
            )
        finally:
            self.rate_limiter.release(user)

    async def handle_reaction_added(self, event: dict[str, Any]) -> None:
        reaction = event.get("reaction")
        if reaction not in {"thumbsup", "+1", "thumbsdown", "-1"}:
            return
        item = event.get("item", {})
        channel = str(item.get("channel", ""))
        message_ts = str(item.get("ts", ""))
        trace_id = self._trace_by_message.get((channel, message_ts))
        if trace_id is None:
            # Not a reaction on a message pro-pilot answered (or the mapping expired);
            # there is no pro-pilot trace to tie this feedback to.
            return
        self.audit.feedback(
            trace_id=trace_id,
            user=str(event.get("user", "")),
            reaction=str(reaction),
        )

    def _remember_trace(self, channel: str, message_ts: str, trace_id: str) -> None:
        if not message_ts:
            return
        key = (channel, message_ts)
        self._trace_by_message[key] = trace_id
        self._trace_by_message.move_to_end(key)
        while len(self._trace_by_message) > _MAX_TRACE_ENTRIES:
            self._trace_by_message.popitem(last=False)

    async def _post(
        self, channel: str, text: str, *, thread_ts: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "channel": channel,
            "text": text,
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if thread_ts:
            params["thread_ts"] = thread_ts
        return await self.client.chat_postMessage(**params)

    async def _update(self, channel: str, ts: str, answer: str, footer: str) -> None:
        fallback_text, blocks = build_answer_payload(answer, footer)
        await self.client.chat_update(
            channel=channel,
            ts=ts,
            text=fallback_text,
            blocks=blocks,
            unfurl_links=False,
            unfurl_media=False,
        )

    async def _ephemeral(self, channel: str, user: str, text: str) -> None:
        await self.client.chat_postEphemeral(
            channel=channel,
            user=user,
            text=text,
            unfurl_links=False,
            unfurl_media=False,
        )


def build_slack_app(handler: SlackHandler, *, bot_token: str) -> Any:
    from slack_bolt.async_app import AsyncApp

    app = AsyncApp(token=bot_token)

    async def on_mention(event: dict[str, Any], client: Any) -> None:
        handler.client = client
        await handler.handle_app_mention(event)

    async def on_reaction(event: dict[str, Any], client: Any) -> None:
        handler.client = client
        await handler.handle_reaction_added(event)

    app.event("app_mention")(on_mention)
    app.event("reaction_added")(on_reaction)
    return app


def _strip_mention(text: str) -> str:
    return re.sub(r"<@[^>]+>", "", text)


def _limit_answer(text: str, limit: int = 3500) -> str:
    if len(text) <= limit:
        return text
    clipped = text[:limit]
    boundary = clipped.rfind("\n")
    return (
        f"{clipped[: boundary if boundary > 0 else limit]}\n"
        "There's more detail than I can fit here. Ask about one application for a shorter answer."
    )


def _footer(result: AgentResult) -> str:
    clusters = {record.cluster for record in result.tools if record.cluster}
    environments = ", ".join(
        escape(cluster.replace("-", " ").replace("_", " ").title(), quote=False)
        for cluster in sorted(clusters)
    )
    feedback = "👍 Helpful · 👎 Needs improvement"
    return f"{environments} · {feedback}" if environments else feedback
