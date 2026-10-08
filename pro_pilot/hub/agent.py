"""Framework-free bounded model tool-calling loop."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from pro_pilot.protocol.messages import ErrorCode, ErrorDetail, ToolResult
from pro_pilot.protocol.tools import TOOL_REGISTRY

from .llm import LLMUnavailable, maybe_await
from .registry import RegistryError, SpokeRegistry


@dataclass(frozen=True)
class AgentConfig:
    max_iterations: int = 8
    max_tool_calls: int = 16
    max_parallel_tool_calls: int = 4
    turn_deadline_s: float = 90
    tool_result_chars_to_llm: int = 12000


@dataclass
class ToolRecord:
    tool: str
    cluster: str | None
    args: dict[str, Any]
    result: ToolResult


@dataclass
class AgentResult:
    text: str
    trace_id: str
    tools: list[ToolRecord] = field(default_factory=list)
    iterations: int = 0
    outcome: str = "answered"
    usage: dict[str, int] = field(default_factory=dict)
    messages: list[dict[str, Any]] = field(default_factory=list)


def render_system_prompt(clusters: list[dict[str, Any]]) -> str:
    prompt_dir = Path(__file__).with_name("prompts")
    environment = Environment(
        loader=FileSystemLoader(prompt_dir),
        undefined=StrictUndefined,
        autoescape=False,
    )
    template = environment.get_template("system.md.j2")
    return template.render(
        clusters=clusters,
        now_utc=datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
    )


class AgentLoop:
    def __init__(
        self,
        llm: Any,
        registry: SpokeRegistry,
        *,
        config: AgentConfig | None = None,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.config = config or AgentConfig()

    async def run(
        self,
        question: str,
        *,
        cluster_ids: list[str],
        history: list[dict[str, Any]] | None = None,
        trace_id: str | None = None,
    ) -> AgentResult:
        trace_id = trace_id or str(uuid.uuid4())
        progress = AgentResult(text="", trace_id=trace_id)
        try:
            return await asyncio.wait_for(
                self._run(
                    question,
                    cluster_ids=cluster_ids,
                    history=history or [],
                    trace_id=trace_id,
                    progress=progress,
                ),
                timeout=self.config.turn_deadline_s,
            )
        except TimeoutError:
            progress.text = (
                ":grey_question: *I couldn't finish this check*\n"
                "Please try again with one application or a smaller area to check."
            )
            progress.outcome = "deadline"
        except LLMUnavailable:
            progress.text = (
                ":grey_question: *I can't answer right now*\nPlease try again in a minute."
            )
            progress.outcome = "llm_error"
        # Interrupted tool-use sequences cannot be replayed in a follow-up request.
        progress.messages = [
            *(history or []),
            {"role": "user", "content": [{"text": question}]},
            {"role": "assistant", "content": [{"text": progress.text}]},
        ]
        return progress

    async def _run(
        self,
        question: str,
        *,
        cluster_ids: list[str],
        history: list[dict[str, Any]],
        trace_id: str,
        progress: AgentResult,
    ) -> AgentResult:
        deadline = asyncio.get_running_loop().time() + self.config.turn_deadline_s
        clusters = self.registry.statuses(cluster_ids)
        messages = [*history, {"role": "user", "content": [{"text": question}]}]
        progress.messages = messages
        tool_names = self._tool_names(cluster_ids)
        records: list[ToolRecord] = []
        progress.tools = records
        total_calls = 0
        usage: dict[str, int] = {}
        for iteration in range(self.config.max_iterations):
            response = await maybe_await(
                self.llm.converse(
                    system=render_system_prompt(clusters),
                    messages=messages,
                    tool_names=tool_names,
                    cluster_ids=cluster_ids,
                    use_tools=True,
                )
            )
            assistant_message = response.get("output", {}).get("message", {})
            messages.append(assistant_message)
            usage = _sum_usage(usage, _usage(response))
            progress.usage = usage
            progress.iterations = iteration + 1
            stop_reason = response.get("stopReason")
            if stop_reason in {"end_turn", "max_tokens"}:
                text = _text_from_message(assistant_message)
                if stop_reason == "max_tokens":
                    text = (
                        f"{text}\nThere's more detail than I can fit here. "
                        "Ask about one application for a shorter answer."
                    )
                return AgentResult(
                    text=text,
                    trace_id=trace_id,
                    tools=records,
                    iterations=iteration + 1,
                    usage=usage,
                    messages=messages,
                )
            if stop_reason != "tool_use":
                return AgentResult(
                    text=(":grey_question: *I couldn't get a clear answer*\nPlease try again."),
                    trace_id=trace_id,
                    tools=records,
                    iterations=iteration + 1,
                    outcome="llm_error",
                    usage=usage,
                    messages=messages,
                )
            calls = _tool_uses(assistant_message)
            allowed = self.config.max_tool_calls - total_calls
            selected = calls[: max(0, allowed)]
            results: list[ToolResult] = []
            for call in calls[len(selected) :]:
                results.append(
                    ToolResult(
                        id=call["toolUseId"],
                        ok=False,
                        error=ErrorDetail(
                            code=ErrorCode.TOOL_BUDGET_EXCEEDED,
                            message="per-question tool-call budget used up",
                        ),
                    )
                )
            if len(selected) < len(calls):
                total_calls = self.config.max_tool_calls
            else:
                total_calls += len(selected)
            bounded = asyncio.Semaphore(self.config.max_parallel_tool_calls)

            async def execute_one(
                call: dict[str, Any],
                semaphore: asyncio.Semaphore = bounded,
            ) -> tuple[dict[str, Any], ToolResult]:
                async with semaphore:
                    result = await self._execute_call(
                        call,
                        cluster_ids=cluster_ids,
                        trace_id=trace_id,
                        deadline_ms=max(
                            1,
                            min(15000, int((deadline - asyncio.get_running_loop().time()) * 1000)),
                        ),
                    )
                    cluster = call.get("input", {}).get("cluster")
                    records.append(
                        ToolRecord(
                            tool=call["name"],
                            cluster=cluster if isinstance(cluster, str) else None,
                            args=call.get("input", {}),
                            result=result,
                        )
                    )
                    return call, result

            executed = await asyncio.gather(*(execute_one(call) for call in selected))
            result_by_id = {call["toolUseId"]: result for call, result in executed}
            budget_results = iter(results)
            for call in calls:
                if call["toolUseId"] not in result_by_id:
                    result_by_id[call["toolUseId"]] = next(budget_results)
            messages.append(
                {
                    "role": "user",
                    "content": [
                        _bedrock_tool_result(
                            call,
                            result_by_id[call["toolUseId"]],
                            self.config.tool_result_chars_to_llm,
                        )
                        for call in calls
                    ],
                }
            )
            progress.messages = messages
            continue
        final = await maybe_await(
            self.llm.converse(
                system=render_system_prompt(clusters),
                messages=messages,
                tool_names=[],
                cluster_ids=cluster_ids,
                use_tools=False,
            )
        )
        message = final.get("output", {}).get("message", {})
        usage = _sum_usage(usage, _usage(final))
        return AgentResult(
            text=_text_from_message(message)
            or (
                ":grey_question: *I couldn't finish this check*\n"
                "Please ask about one application or a smaller area to check."
            ),
            trace_id=trace_id,
            tools=records,
            iterations=self.config.max_iterations,
            outcome="answered",
            usage=usage,
            messages=messages + [message],
        )

    def _tool_names(self, cluster_ids: list[str]) -> list[str]:
        names = ["list_clusters"]
        for name in TOOL_REGISTRY:
            if name == "list_clusters":
                continue
            if any(
                (connection := self.registry.get(cluster_id)) is not None
                and name in connection.hello.tools
                for cluster_id in cluster_ids
            ):
                names.append(name)
        return names

    async def _execute_call(
        self,
        call: dict[str, Any],
        *,
        cluster_ids: list[str],
        trace_id: str,
        deadline_ms: int = 15000,
    ) -> ToolResult:
        tool = call.get("name")
        input_data = call.get("input", {})
        if tool == "list_clusters":
            return ToolResult(
                id=call["toolUseId"], ok=True, data=self.registry.statuses(cluster_ids)
            )
        if tool not in TOOL_REGISTRY or not isinstance(input_data, dict):
            return ToolResult(
                id=call["toolUseId"],
                ok=False,
                error=ErrorDetail(code=ErrorCode.INVALID_ARGS, message="invalid tool call"),
            )
        cluster = input_data.get("cluster")
        if not isinstance(cluster, str) or cluster not in cluster_ids:
            return ToolResult(
                id=call["toolUseId"],
                ok=False,
                error=ErrorDetail(
                    code=ErrorCode.CLUSTER_NOT_ALLOWED,
                    message="cluster is not mapped to this Slack channel",
                ),
            )
        args = {key: value for key, value in input_data.items() if key != "cluster"}
        try:
            result = await self.registry.call(
                cluster_id=cluster,
                tool=tool,
                args=args,
                trace_id=trace_id,
                deadline_ms=deadline_ms,
            )
            result.id = call["toolUseId"]
            return result
        except RegistryError as exc:
            return ToolResult(
                id=call["toolUseId"],
                ok=False,
                error=ErrorDetail(code=exc.code, message=exc.message),
            )


def _tool_uses(message: dict[str, Any]) -> list[dict[str, Any]]:
    return [block["toolUse"] for block in message.get("content", []) if "toolUse" in block]


def _text_from_message(message: dict[str, Any]) -> str:
    return "\n".join(block["text"] for block in message.get("content", []) if "text" in block)


def _usage(response: dict[str, Any]) -> dict[str, int]:
    usage = response.get("usage", {})
    return {key: int(usage[key]) for key in ("inputTokens", "outputTokens") if key in usage}


def _sum_usage(base: dict[str, int], addition: dict[str, int]) -> dict[str, int]:
    """Sum per-call token usage across model round trips."""
    result = dict(base)
    for key, value in addition.items():
        result[key] = result.get(key, 0) + value
    return result


def _bedrock_tool_result(call: dict[str, Any], result: ToolResult, limit: int) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "cluster": call.get("input", {}).get("cluster"),
        "ok": result.ok,
        "truncated": result.truncated,
    }
    if result.ok:
        payload["data"] = result.data
    elif result.error is not None:
        payload["error"] = result.error.model_dump(mode="json")
    content = {"json": payload}
    encoded = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > limit:
        summary = {"cluster": payload["cluster"], "ok": result.ok, "truncated": True, "summary": ""}
        lo, hi = 0, len(encoded)
        while lo < hi:
            middle = (lo + hi + 1) // 2
            summary["summary"] = encoded[:middle]
            if len(json.dumps({"json": summary}, ensure_ascii=False)) <= limit:
                lo = middle
            else:
                hi = middle - 1
        summary["summary"] = encoded[:lo]
        content = {"json": summary}
    return {
        "toolResult": {
            "toolUseId": call["toolUseId"],
            "content": [content],
            "status": "success" if result.ok else "error",
        }
    }
