"""Chat Completions transport for the hub's existing conversation envelope."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Callable
from typing import Any

import aiohttp

from pro_pilot.protocol.tools import TOOL_REGISTRY, tool_schema

from .llm import LLMUnavailable

LOGGER = logging.getLogger(__name__)
_MAX_RESPONSE_BYTES = 1024 * 1024
_USER_AGENT = "Mozilla/5.0 (compatible; pro-pilot/0.1)"


class OpenAIClient:
    def __init__(
        self,
        *,
        base_url: str,
        model_id: str,
        api_key: str,
        max_output_tokens: int = 2000,
        temperature: float = 0,
        session: aiohttp.ClientSession | None = None,
        random_fn: Callable[[], float] = random.random,
    ) -> None:
        if not api_key:
            raise ValueError("PRO_PILOT_LLM_API_KEY is required for llm.mode=openai")
        self.base_url = base_url.rstrip("/")
        self.model_id = model_id
        self.max_output_tokens = max_output_tokens
        self.temperature = temperature
        self._api_key = api_key
        self._session = session
        self._owns_session = session is None
        self._random = random_fn

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def converse(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tool_names: list[str],
        cluster_ids: list[str],
        use_tools: bool = True,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": self.model_id,
            "messages": [{"role": "system", "content": system}, *_chat_messages(messages)],
            "max_tokens": self.max_output_tokens,
            "temperature": self.temperature,
            "stream": False,
        }
        if use_tools and tool_names:
            request["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": TOOL_REGISTRY[name].description,
                        "parameters": tool_schema(TOOL_REGISTRY[name], cluster_ids),
                    },
                }
                for name in tool_names
            ]
            request["tool_choice"] = "auto"
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        for attempt, delay in enumerate((0, 1, 3)):
            if delay:
                await asyncio.sleep(delay + self._random() * 0.2 * delay)
            try:
                async with self._session.post(
                    f"{self.base_url}/chat/completions",
                    json=request,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Accept": "application/json",
                        "User-Agent": _USER_AGENT,
                    },
                    allow_redirects=False,
                ) as response:
                    if response.status != 200:
                        LOGGER.warning(
                            "llm_http_error",
                            extra={"status": response.status, "attempt": attempt + 1},
                        )
                        if response.status in {429, 503} and attempt < 2:
                            continue
                        raise LLMUnavailable(f"Model API returned HTTP {response.status}")
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                            raise LLMUnavailable("Model response exceeded the byte limit")
                        body.extend(chunk)
                    return _model_response(json.loads(body))
            except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError) as exc:
                LOGGER.warning("llm_api_error", extra={"error_type": type(exc).__name__})
                raise LLMUnavailable("Model API request or response failed") from exc
        raise LLMUnavailable("Model API is unavailable")


def _chat_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for message in messages:
        blocks = message["content"]
        text = "\n".join(block["text"] for block in blocks if "text" in block)
        results = [block["toolResult"] for block in blocks if "toolResult" in block]
        calls = [block["toolUse"] for block in blocks if "toolUse" in block]
        if results:
            for result in results:
                content = "\n".join(
                    json.dumps(block["json"], ensure_ascii=False)
                    if "json" in block
                    else block["text"]
                    for block in result["content"]
                )
                converted.append(
                    {
                        "role": "tool",
                        "tool_call_id": result["toolUseId"],
                        "content": content,
                    }
                )
            if not text:
                continue
        chat_message: dict[str, Any] = {"role": message["role"], "content": text or None}
        if calls:
            native_calls = []
            for call in calls:
                native: dict[str, Any] = {
                    "id": call["toolUseId"],
                    "type": "function",
                    "function": {"name": call["name"], "arguments": json.dumps(call["input"])},
                }
                metadata = call.get("providerMetadata", {})
                if "extra_content" in metadata:
                    native["extra_content"] = metadata["extra_content"]
                native_calls.append(native)
            chat_message["tool_calls"] = native_calls
        converted.append(chat_message)
    return converted


def _model_response(payload: Any) -> dict[str, Any]:
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("choices"), list)
        or not payload["choices"]
    ):
        raise LLMUnavailable("Model response must contain a choice")
    choice = payload["choices"][0]
    if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
        raise LLMUnavailable("Model choice must contain a message")
    message = choice["message"]
    if message.get("role") != "assistant":
        raise LLMUnavailable("Model response role must be assistant")
    blocks: list[dict[str, Any]] = []
    text = message.get("content")
    if text is not None and not isinstance(text, str):
        raise LLMUnavailable("Model text must be a string")
    if text:
        blocks.append({"text": text})
    ids: set[str] = set()
    calls = message.get("tool_calls") or []
    if not isinstance(calls, list):
        raise LLMUnavailable("Model tool calls must be a list")
    for call in calls:
        if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
            raise LLMUnavailable("Invalid model function call")
        call_id = call["id"]
        function = call["function"]
        args = json.loads(function["arguments"])
        if (
            call.get("type") != "function"
            or not isinstance(call_id, str)
            or not call_id
            or call_id in ids
            or not isinstance(function["name"], str)
            or not isinstance(args, dict)
        ):
            raise LLMUnavailable("Invalid model function call")
        ids.add(call_id)
        tool_use = {"toolUseId": call_id, "name": function["name"], "input": args}
        if "extra_content" in call:
            tool_use["providerMetadata"] = {"extra_content": call["extra_content"]}
        blocks.append({"toolUse": tool_use})
    if not blocks:
        raise LLMUnavailable("Model returned no text or tool calls")
    finish = choice.get("finish_reason")
    if not isinstance(finish, str):
        raise LLMUnavailable("Model completion reason must be a string")
    if finish == "tool_calls" and not ids:
        raise LLMUnavailable("Tool-call completion must contain a function call")
    stop_reason = {
        "tool_calls": "tool_use",
        "stop": "end_turn",
        "length": "max_tokens",
    }.get(finish, finish)
    usage = payload.get("usage") or {}
    if not isinstance(usage, dict):
        raise LLMUnavailable("Invalid model usage")
    return {
        "output": {"message": {"role": "assistant", "content": blocks}},
        "stopReason": stop_reason,
        "usage": {
            "inputTokens": int(usage.get("prompt_tokens", 0)),
            "outputTokens": int(usage.get("completion_tokens", 0)),
        },
    }
