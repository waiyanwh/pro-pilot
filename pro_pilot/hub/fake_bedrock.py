"""Fixture-driven Bedrock substitute for Phase 0 local Slack (no AWS)."""

from __future__ import annotations

import json
import uuid
from typing import Any


class FakeBedrockClient:
    """Minimal agent driver: calls spoke tools, then formats a Slack-shaped answer."""

    model_id = "fake"

    async def converse(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tool_names: list[str],
        cluster_ids: list[str],
        use_tools: bool = True,
    ) -> dict[str, Any]:
        del system, tool_names
        if not use_tools:
            return _end_turn(_answer_from_messages(messages))
        if _has_tool_results(messages):
            return _end_turn(_answer_from_messages(messages))
        cluster = cluster_ids[0] if cluster_ids else "demo-cluster"
        question = _last_user_text(messages).lower()
        tool, args = _plan_tool(question, cluster)
        return _tool_use(tool, args)


def _plan_tool(question: str, cluster: str) -> tuple[str, dict[str, Any]]:
    if any(token in question for token in ("crash", "oom", "payment-api", "payment")):
        if "log" in question:
            return "get_pod_logs", {
                "cluster": cluster,
                "namespace": "payment",
                "pod": "payment-api-7d9c-abcde",
                "previous": True,
            }
        return "get_pods", {"cluster": cluster, "namespace": "payment"}
    return "list_namespaces", {"cluster": cluster}


def _tool_use(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "stopReason": "tool_use",
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": uuid.uuid4().hex,
                            "name": name,
                            "input": args,
                        }
                    }
                ],
            }
        },
    }


def _end_turn(text: str) -> dict[str, Any]:
    return {
        "stopReason": "end_turn",
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
    }


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        parts = []
        for block in message.get("content", []):
            if "text" in block:
                parts.append(str(block["text"]))
        if parts:
            return "\n".join(parts)
    return ""


def _has_tool_results(messages: list[dict[str, Any]]) -> bool:
    for message in messages:
        for block in message.get("content", []):
            if "toolResult" in block:
                return True
    return False


def _answer_from_messages(messages: list[dict[str, Any]]) -> str:
    pods: list[dict[str, Any]] | None = None
    logs: dict[str, Any] | None = None
    for message in messages:
        for block in message.get("content", []):
            tool_result = block.get("toolResult")
            if not tool_result:
                continue
            payload = _tool_payload(tool_result)
            if not payload.get("ok"):
                error = payload.get("error", {})
                code = error.get("code", "ERROR")
                msg = error.get("message", "lookup failed")
                if code == "CLUSTER_UNAVAILABLE":
                    return (
                        ":warning: The demo-cluster agent is offline right now. "
                        "Try again in a minute."
                    )
                return f":warning: Lookup failed ({code}): {msg}"
            data = payload.get("data")
            if (
                isinstance(data, list)
                and data
                and isinstance(data[0], dict)
                and ("last_termination" in data[0] or "waiting_reason" in data[0])
            ):
                pods = data
            if isinstance(data, dict) and "lines" in data:
                logs = data
    if pods:
        return _format_crashloop_answer(pods, logs)
    return (
        ":grey_question: I checked the fixture cluster but didn't find a clear pattern. "
        "Try asking about payment-api crashlooping."
    )


def _tool_payload(tool_result: dict[str, Any]) -> dict[str, Any]:
    content = tool_result.get("content") or []
    if not content:
        return {}
    block = content[0]
    if "json" in block:
        raw = block["json"]
        return raw if isinstance(raw, dict) else {}
    if "text" in block:
        try:
            parsed = json.loads(str(block["text"]))
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _format_crashloop_answer(pods: list[dict[str, Any]], logs: dict[str, Any] | None) -> str:
    pod = pods[0]
    name = str(pod.get("name", "payment-api"))
    restarts = pod.get("restarts", "?")
    waiting = pod.get("waiting_reason", "CrashLoopBackOff")
    term = pod.get("last_termination") or {}
    reason = term.get("reason", "OOMKilled")
    exit_code = term.get("exit_code", 137)
    finished = term.get("finished_at", "")
    lines = [
        (
            f":red_circle: `{name}` is crash-looping because the container was "
            f"*{reason}* (memory limit 512Mi on the fixture deployment)."
        ),
        "",
        "*What I found*",
        (
            f"• `payment/pod/{name}`: {waiting}, {restarts} restarts, last termination "
            f"`{reason}` (exit {exit_code})" + (f" at {finished}" if finished else "")
        ),
    ]
    if logs:
        excerpt = " ".join(str(line) for line in logs.get("lines", [])[:2])
        if excerpt:
            lines.append(f"• Previous container logs: `{excerpt[:180]}`")
    else:
        lines.append(
            "• Previous container logs end with `java.lang.OutOfMemoryError: Java heap space`"
        )
    lines.extend(
        [
            "",
            "*What to do next*",
            (
                "• Compare memory limits on `payment/deployment/payment-api` "
                "with the last healthy revision"
            ),
            "• Ask your platform team to adjust Helm or JVM heap — pro-pilot cannot change the cluster",
        ]
    )
    return "\n".join(lines)
