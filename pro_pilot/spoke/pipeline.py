"""Spoke execution pipeline: lookup, validation, policy, execution, projection,
and cap.

Step order follows spec/spoke.md "Tool execution pipeline" exactly:
lookup -> validate -> policy -> execute -> project -> redact -> cap -> respond.
Namespace filtering for cluster-wide list/get calls runs on the *raw* executor
output, before projection, so a denied or malformed object is dropped without
ever being validated against (or leaked through) the output model. Redaction
always runs before any content truncation (byte cap, log line cap) so a cut
can never split a secret and leave part of it unredacted.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from pydantic import ValidationError

from pro_pilot.protocol.messages import ErrorCode, ErrorDetail, ToolResult
from pro_pilot.protocol.tools import TOOL_REGISTRY, ToolDefinition

from .execution import DEADLINE, ExecutionResult
from .policy import NamespacePolicy
from .project import project_output
from .redact import redact, redact_text

_LOGGER = logging.getLogger("pro_pilot.spoke")


def _default_log_call(record: dict[str, Any]) -> None:
    """Emit the spec/spoke.md "Logging" line when no custom sink is wired.

    Never includes result data (only the fields spec/spoke.md lists), and
    redacts the record itself before serializing, so a selector or arg value
    that happens to look like a credential still never reaches stdout
    unredacted. `pro_pilot.logging.configure_logging()` renders this (and
    any custom `log_call`'s own logger calls) as structured JSON.
    """

    safe_record, _ = redact({"event": "spoke.tool_call", **record})
    _LOGGER.info(json.dumps(safe_record, ensure_ascii=False, separators=(",", ":")))


class ToolExecutor(Protocol):
    async def execute(self, tool_name: str, args: dict[str, Any]) -> Any:
        """Execute one already-validated call."""


class ToolFailure(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


LogCall = Callable[[dict[str, Any]], None | Awaitable[None]]

_LOG_LINE_MAX_CHARS = 1000
_QUEUE_WAIT_CAP_S = 2.0


class PipelineConfig:
    def __init__(
        self,
        *,
        tool_timeout_s: float = 15,
        max_result_bytes: int = 32768,
        max_list_items: int = 200,
        max_concurrent_tools: int = 4,
    ) -> None:
        if min(tool_timeout_s, max_result_bytes, max_list_items, max_concurrent_tools) <= 0:
            raise ValueError("spoke limits must be positive")
        self.tool_timeout_s = tool_timeout_s
        self.max_result_bytes = max_result_bytes
        self.max_list_items = max_list_items
        self.max_concurrent_tools = max_concurrent_tools


class ToolPipeline:
    def __init__(
        self,
        executor: ToolExecutor,
        policy: NamespacePolicy,
        *,
        advertised_tools: set[str] | None = None,
        config: PipelineConfig | None = None,
        log_call: LogCall | None = None,
        cluster_id: str | None = None,
    ) -> None:
        self.executor = executor
        self.policy = policy
        self.advertised_tools = (
            set(TOOL_REGISTRY) - {"list_clusters"} if advertised_tools is None else advertised_tools
        )
        self.config = config or PipelineConfig()
        self._semaphore = asyncio.Semaphore(self.config.max_concurrent_tools)
        # A default sink is always present (spec/spoke.md "Logging" requires one
        # JSON line per tool call); callers may still supply their own to route
        # the same record elsewhere in addition to or instead of stdout.
        self._log_call = log_call if log_call is not None else _default_log_call
        self.cluster_id = cluster_id

    async def execute(
        self,
        call_id: str,
        tool_name: str,
        raw_args: dict[str, Any],
        *,
        deadline_ms: int,
        trace_id: str = "",
    ) -> ToolResult:
        started = time.monotonic()
        error_code: ErrorCode | None = None
        redactions = 0
        truncated = False
        bytes_sent = 0
        definition = TOOL_REGISTRY.get(tool_name)
        validated_args: dict[str, Any] = {}
        try:
            if definition is None or tool_name not in self.advertised_tools:
                raise ToolFailure(
                    ErrorCode.UNKNOWN_TOOL, f"tool {tool_name!r} is not advertised by this spoke"
                )
            args = self._validate(definition, raw_args)
            validated_args = args
            denied_namespace = self._check_policy(tool_name, args)
            if denied_namespace is not None:
                raise ToolFailure(
                    ErrorCode.NOT_ALLOWED, self.policy.denied_message(denied_namespace)
                )
            deadline_s = max(0.001, deadline_ms / 1000)
            timeout_s = min(self.config.tool_timeout_s, deadline_s)
            deadline_at = started + timeout_s
            raw = await self._execute_with_limit(tool_name, args, deadline_at)
            upstream_truncated = False
            if isinstance(raw, ExecutionResult):
                upstream_truncated = raw.truncated
                raw = raw.data
            raw = _filter_log_lines(tool_name, args, raw)
            raw, list_truncated = self._filter_raw_by_policy(tool_name, raw)
            projected = project_output(definition.output_model, raw)
            redacted, redactions = redact(projected)
            redacted, line_truncated = _cap_log_line_length(tool_name, redacted)
            capped, byte_truncated = _cap_payload(redacted, self.config.max_result_bytes)
            truncated = (
                upstream_truncated
                or byte_truncated
                or line_truncated
                or list_truncated
                or (isinstance(capped, dict) and bool(capped.get("truncated", False)))
            )
            bytes_sent = len(_json_bytes(capped))
            return ToolResult(
                id=call_id,
                ok=True,
                data=capped,
                truncated=truncated,
                redactions=redactions,
                duration_ms=_duration_ms(started),
            )
        except ToolFailure as exc:
            error_code = exc.code
            return self._failure(call_id, exc.code, exc.message, started)
        except ValidationError as exc:
            error_code = ErrorCode.INVALID_ARGS
            return self._failure(call_id, ErrorCode.INVALID_ARGS, _validation_message(exc), started)
        except TimeoutError:
            error_code = ErrorCode.TIMEOUT
            return self._failure(call_id, ErrorCode.TIMEOUT, "tool deadline exceeded", started)
        except PermissionError:
            error_code = ErrorCode.FORBIDDEN
            return self._failure(call_id, ErrorCode.FORBIDDEN, "read access was denied", started)
        except FileNotFoundError:
            error_code = ErrorCode.NOT_FOUND
            return self._failure(call_id, ErrorCode.NOT_FOUND, "resource was not found", started)
        except Exception as exc:  # noqa: BLE001 - executor boundaries need a safe public error
            error_code = ErrorCode.UPSTREAM_ERROR
            lines = str(exc).splitlines()
            message, redactions = redact_text(lines[0] if lines else "upstream error")
            message = message[:500]
            return self._failure(call_id, ErrorCode.UPSTREAM_ERROR, message, started)
        finally:
            record = {
                "cluster_id": self.cluster_id,
                "trace_id": trace_id,
                "tool": tool_name,
                "args": validated_args,
                "ok": error_code is None,
                "error_code": error_code.value if error_code else None,
                "duration_ms": _duration_ms(started),
                "bytes": bytes_sent,
                "redactions": redactions,
                "truncated": truncated,
            }
            result = self._log_call(record)
            if asyncio.iscoroutine(result):
                await result

    def _validate(self, definition: ToolDefinition, raw_args: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw_args, dict):
            raise ToolFailure(ErrorCode.INVALID_ARGS, "tool arguments must be an object")
        model = definition.args_model.model_validate(raw_args)
        return model.model_dump(mode="json", exclude_none=True)

    def _check_policy(self, tool_name: str, args: dict[str, Any]) -> str | None:
        if tool_name == "get_application":
            return None
        return self.policy.check_tool_args(tool_name, args)

    async def _execute_with_limit(
        self, tool_name: str, args: dict[str, Any], deadline_at: float
    ) -> Any:
        """Run the executor call, enforcing one deadline that includes queue time.

        ``deadline_at`` is an absolute ``time.monotonic()`` instant computed
        from the call's overall deadline. Time spent waiting for a free
        concurrency slot is *not* refunded to the execution phase: the total
        wall time from call start to result can never exceed the deadline.
        """

        queue_wait = max(0.0, min(_QUEUE_WAIT_CAP_S, deadline_at - time.monotonic()))
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=queue_wait)
        except TimeoutError as exc:
            if queue_wait >= _QUEUE_WAIT_CAP_S:
                raise ToolFailure(
                    ErrorCode.RATE_LIMITED, "spoke tool concurrency limit is full"
                ) from exc
            raise  # the overall deadline, not congestion, was the limiting factor
        try:
            remaining = deadline_at - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            deadline_token = DEADLINE.set(deadline_at)
            try:
                return await asyncio.wait_for(
                    self.executor.execute(tool_name, args), timeout=remaining
                )
            finally:
                DEADLINE.reset(deadline_token)
        finally:
            self._semaphore.release()

    def _filter_raw_by_policy(self, tool_name: str, raw: Any) -> tuple[Any, bool]:
        """Drop denied or out-of-scope objects from the raw executor output,
        before projection, per spec/spoke.md step 3."""

        if isinstance(raw, list):
            items = [item for item in raw if isinstance(item, dict)]
            if len(items) != len(raw):
                items = raw
            filtered = (
                self.policy.filter_items(tool_name, items)
                if items and isinstance(items[0], dict)
                else items
            )
            return filtered[: self.config.max_list_items], len(
                filtered
            ) > self.config.max_list_items
        if (
            tool_name == "get_application"
            and isinstance(raw, dict)
            and not self.policy.application_allowed(raw)
        ):
            namespace = raw.get("destination_namespace", "unknown")
            raise ToolFailure(ErrorCode.NOT_ALLOWED, self.policy.denied_message(str(namespace)))
        return raw, False

    def _failure(self, call_id: str, code: ErrorCode, message: str, started: float) -> ToolResult:
        clean_message, _ = redact_text(message)
        return ToolResult(
            id=call_id,
            ok=False,
            error=ErrorDetail(code=code, message=clean_message),
            duration_ms=_duration_ms(started),
        )


def _filter_log_lines(tool_name: str, args: dict[str, Any], raw: Any) -> Any:
    """Apply ``contains`` and ``tail_lines`` to ``get_pod_logs`` raw lines.

    This runs on raw (pre-redaction) text: selecting which whole lines survive
    cannot split a secret the way content truncation can, so there is no
    ordering requirement against redaction here.
    """

    if tool_name != "get_pod_logs" or not isinstance(raw, dict):
        return raw
    lines = raw.get("lines")
    if not isinstance(lines, list):
        return raw
    contains = args.get("contains")
    if isinstance(contains, str) and contains:
        needle = contains.lower()
        lines = [line for line in lines if isinstance(line, str) and needle in line.lower()]
    tail_lines = args.get("tail_lines", 100)
    new_raw = dict(raw)
    if isinstance(tail_lines, int) and len(lines) > tail_lines:
        lines = lines[-tail_lines:]
        new_raw["truncated"] = True
    new_raw["lines"] = lines
    return new_raw


def _cap_log_line_length(tool_name: str, data: Any) -> tuple[Any, bool]:
    """Cap each log line to 1,000 chars, after redaction (spec/tools.md #7)."""

    if tool_name != "get_pod_logs" or not isinstance(data, dict):
        return data, False
    lines = data.get("lines")
    if not isinstance(lines, list):
        return data, False
    truncated = False
    capped_lines: list[Any] = []
    for line in lines:
        if isinstance(line, str) and len(line) > _LOG_LINE_MAX_CHARS:
            capped_lines.append(line[:_LOG_LINE_MAX_CHARS])
            truncated = True
        else:
            capped_lines.append(line)
    if not truncated:
        return data, False
    capped = dict(data)
    capped["lines"] = capped_lines
    capped["truncated"] = True
    return capped, True


def _validation_message(error: ValidationError) -> str:
    first = error.errors()[0]
    location = ".".join(str(part) for part in first.get("loc", ())) or "arguments"
    return f"invalid {location}: {first.get('msg', 'invalid value')}"


def _duration_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def _iter_strings(value: Any) -> list[tuple[Any, Any, str]]:
    """Collect every ``(container, key_or_index, string_value)`` leaf in a
    JSON-compatible structure, so the largest one can be shrunk in place
    without ever hand-slicing already-JSON-encoded bytes (which would land
    mid-escape-sequence and corrupt the document)."""

    found: list[tuple[Any, Any, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str):
                found.append((value, key, item))
            else:
                found.extend(_iter_strings(item))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, str):
                found.append((value, index, item))
            else:
                found.extend(_iter_strings(item))
    return found


def _shrink_longest_string(candidate: Any) -> bool:
    strings = _iter_strings(candidate)
    if not strings:
        return False
    container, key, value = max(strings, key=lambda item: len(item[2]))
    if not value:
        return False
    container[key] = value[: len(value) // 2]
    return True


def _cap_payload(value: Any, max_bytes: int) -> tuple[Any, bool]:
    """Shrink ``value`` until its JSON encoding fits ``max_bytes``.

    Structural truncation (dropping list items, keeping the last log lines)
    is tried first. If a single scalar field is itself the oversized part
    (e.g. one long message string with no lists left to drop), the longest
    string value anywhere in the structure is shrunk in place by character
    count and re-measured by re-encoding - never by slicing the raw JSON
    bytes of the whole document, which would silently produce an invalid or
    mis-escaped result for multi-byte or already-escaped content.
    """

    candidate = copy.deepcopy(value)
    if len(_json_bytes(candidate)) <= max_bytes:
        return candidate, False
    truncated = False
    while len(_json_bytes(candidate)) > max_bytes:
        changed = False
        if isinstance(candidate, dict):
            lines = candidate.get("lines")
            if isinstance(lines, list) and len(lines) > 1:
                lines.pop(0)
                changed = True
            else:
                for item in candidate.values():
                    if isinstance(item, list) and item:
                        item.pop()
                        changed = True
                        break
        elif isinstance(candidate, list) and candidate:
            candidate.pop()
            changed = True
        if not changed:
            changed = _shrink_longest_string(candidate)
        if not changed:
            raise ToolFailure(
                ErrorCode.UPSTREAM_ERROR, "tool result cannot fit the configured byte limit"
            )
        truncated = True
    if isinstance(candidate, dict) and "truncated" in candidate and truncated:
        candidate["truncated"] = True
    return candidate, truncated
