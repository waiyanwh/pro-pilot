"""Validated JSON messages exchanged by the hub and spokes."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .version import PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS


class ProtocolError(ValueError):
    """Raised when an incoming frame is not a supported protocol message."""


class ErrorCode(StrEnum):
    UNKNOWN_TOOL = "UNKNOWN_TOOL"
    INVALID_ARGS = "INVALID_ARGS"
    NOT_ALLOWED = "NOT_ALLOWED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    TIMEOUT = "TIMEOUT"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    CLUSTER_NOT_ALLOWED = "CLUSTER_NOT_ALLOWED"
    CLUSTER_UNAVAILABLE = "CLUSTER_UNAVAILABLE"
    TOOL_NOT_SUPPORTED = "TOOL_NOT_SUPPORTED"
    TOOL_BUDGET_EXCEEDED = "TOOL_BUDGET_EXCEEDED"


class CloseCode:
    NORMAL = 1000
    BAD_HELLO = 4400
    AUTH_FAILED = 4401
    HELLO_TIMEOUT = 4408
    REPLACED = 4409
    PROTOCOL_UNSUPPORTED = 4426


class MessageModel(BaseModel):
    """Wire messages accept additive metadata within the same protocol version."""

    model_config = ConfigDict(extra="ignore", use_enum_values=True)
    v: Literal[1] = PROTOCOL_VERSION


class SpokeLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_result_bytes: int = Field(default=32768, ge=1)
    tool_timeout_s: float = Field(default=15, gt=0)


class Hello(MessageModel):
    type: Literal["hello"] = "hello"
    cluster_id: str = Field(min_length=3, max_length=40, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    spoke_version: str = Field(min_length=1, max_length=64)
    labels: dict[str, str] = Field(default_factory=dict)
    capabilities: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    limits: SpokeLimits = Field(default_factory=SpokeLimits)


class Welcome(MessageModel):
    type: Literal["welcome"] = "welcome"
    session_id: str = Field(min_length=1)
    heartbeat_s: int = Field(default=20, ge=1)


class ToolCall(MessageModel):
    type: Literal["tool_call"] = "tool_call"
    id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)
    deadline_ms: int = Field(gt=0)

    @model_validator(mode="before")
    @classmethod
    def forbid_cluster_routing(cls, payload: Any) -> Any:
        if isinstance(payload, dict) and "cluster" in payload:
            raise ValueError("cluster routing is not part of a spoke tool call")
        return cast(Any, payload)


class ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str = Field(min_length=1, max_length=1000)


class ToolResult(MessageModel):
    type: Literal["tool_result"] = "tool_result"
    id: str = Field(min_length=1)
    ok: bool
    data: Any | None = None
    error: ErrorDetail | None = None
    truncated: bool = False
    redactions: int = Field(default=0, ge=0)
    duration_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_payload(self) -> ToolResult:
        if self.ok and self.error is not None:
            raise ValueError("successful tool results cannot contain an error")
        if not self.ok and self.error is None:
            raise ValueError("failed tool results must contain an error")
        if not self.ok and self.data is not None:
            raise ValueError("failed tool results cannot contain data")
        if self.ok and self.data is None:
            raise ValueError("successful tool results must contain data")
        return self


Message = Hello | Welcome | ToolCall | ToolResult
_MESSAGE_TYPES = {
    "hello": Hello,
    "welcome": Welcome,
    "tool_call": ToolCall,
    "tool_result": ToolResult,
}


def parse_message(frame: str | bytes | dict[str, Any]) -> Message:
    """Parse one text frame into its concrete protocol model."""

    if isinstance(frame, bytes):
        raise ProtocolError("binary frames are not supported")
    try:
        payload = json.loads(frame) if isinstance(frame, str) else frame
    except json.JSONDecodeError as exc:
        raise ProtocolError("invalid JSON frame") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("protocol frame must be a JSON object")
    payload = cast(dict[str, Any], payload)
    version = payload.get("v")
    if type(version) is not int or version not in SUPPORTED_PROTOCOL_VERSIONS:
        raise ProtocolError("unsupported protocol version")
    message_type = payload.get("type")
    if not isinstance(message_type, str):
        raise ProtocolError("message type must be a string")
    model = _MESSAGE_TYPES.get(message_type)
    if model is None:
        raise ProtocolError("unknown message type")
    try:
        return model.model_validate(payload)
    except ValueError as exc:
        raise ProtocolError("invalid protocol message fields") from exc


def encode_message(message: Message) -> str:
    """Serialize a validated message as one JSON text frame."""

    return message.model_dump_json(exclude_none=True)
