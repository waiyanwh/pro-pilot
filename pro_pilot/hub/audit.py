"""Structured audit records without tool-result payloads."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from pro_pilot.spoke.redact import redact, redact_text

LOGGER = logging.getLogger("pro_pilot.audit")


class AuditLog:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.logger = logger or LOGGER

    def question(
        self,
        *,
        trace_id: str,
        slack_user: str,
        channel: str,
        thread_ts: str,
        question: str,
        clusters: list[str],
        tool_calls: list[dict[str, Any]],
        llm: dict[str, Any],
        outcome: str,
        duration_ms: int,
    ) -> dict[str, Any]:
        clean_question, _ = redact_text(question)
        record = {
            "event": "audit.question",
            "trace_id": trace_id,
            "slack_user": slack_user,
            "channel": channel,
            "thread_ts": thread_ts,
            "question": clean_question,
            "clusters": clusters,
            "tool_calls": [
                {
                    "tool": call.get("tool"),
                    "cluster": call.get("cluster"),
                    "args": redact(call.get("args", {}))[0],
                    "ok": call.get("ok"),
                    "error_code": call.get("error_code"),
                    "duration_ms": call.get("duration_ms", 0),
                    "bytes": call.get("bytes", 0),
                    "redactions": call.get("redactions", 0),
                }
                for call in tool_calls
            ],
            "llm": llm,
            "outcome": outcome,
            "duration_ms": duration_ms,
        }
        self.logger.info(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        return record

    def feedback(self, *, trace_id: str, user: str, reaction: str) -> dict[str, Any]:
        record = {
            "event": "audit.feedback",
            "trace_id": trace_id,
            "user": user,
            "reaction": reaction,
        }
        self.logger.info(json.dumps(record, separators=(",", ":")))
        return record


def elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))
