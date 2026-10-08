"""In-memory TTL/LRU Slack thread history."""

from __future__ import annotations

import copy
import json
import time
from collections import OrderedDict
from typing import Any


class ThreadStore:
    def __init__(
        self, *, ttl_s: float = 86400, max_turns: int = 20, max_entries: int = 1000
    ) -> None:
        self.ttl_s = ttl_s
        self.max_turns = max_turns
        self.max_entries = max_entries
        self._entries: OrderedDict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = (
            OrderedDict()
        )

    def get(self, channel_id: str, thread_ts: str) -> list[dict[str, Any]]:
        key = (channel_id, thread_ts)
        entry = self._entries.get(key)
        if entry is None:
            return []
        expires_at, messages = entry
        if expires_at <= time.monotonic():
            self._entries.pop(key, None)
            return []
        self._entries.move_to_end(key)
        return copy.deepcopy(messages)

    def put(self, channel_id: str, thread_ts: str, messages: list[dict[str, Any]]) -> None:
        trimmed = _trim_messages(messages, self.max_turns)
        key = (channel_id, thread_ts)
        self._entries[key] = (time.monotonic() + self.ttl_s, copy.deepcopy(trimmed))
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)


def _trim_messages(messages: list[dict[str, Any]], max_turns: int) -> list[dict[str, Any]]:
    result = copy.deepcopy(messages)
    question_turns = [
        index
        for index, message in enumerate(result)
        if message.get("role") == "user"
        and any("text" in block for block in message.get("content", []))
    ]
    if len(question_turns) > max_turns:
        result = result[question_turns[-max_turns] :]
    for message in result:
        if not _has_tool_result(message):
            continue
        for block in message.get("content", []):
            tool_result = block.get("toolResult")
            if not isinstance(tool_result, dict):
                continue
            content = tool_result.get("content", [])
            encoded = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
            if len(encoded) > 2000:
                # Replace only the content payload; toolUseId/status stay intact so the
                # trimmed history is still a structurally valid Bedrock Converse message.
                tool_result["content"] = [
                    {"text": f"{encoded[:2000]}…(truncated for thread history)"}
                ]
    return result


def _has_tool_result(message: dict[str, Any]) -> bool:
    return any("toolResult" in block for block in message.get("content", []))
