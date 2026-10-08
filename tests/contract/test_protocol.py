import json

import pytest
from pydantic import ValidationError

from pro_pilot.protocol.messages import (
    ErrorCode,
    ErrorDetail,
    Hello,
    ProtocolError,
    ToolResult,
    encode_message,
    parse_message,
)


def test_protocol_messages_round_trip_as_json() -> None:
    message = Hello(
        cluster_id="demo-cluster",
        spoke_version="0.1.0",
        capabilities=["k8s"],
        tools=["get_pods"],
    )
    decoded = parse_message(encode_message(message))
    assert decoded == message


def test_tool_result_requires_matching_success_payload() -> None:
    with pytest.raises(ValidationError):
        ToolResult(id="call", ok=False, data={})
    with pytest.raises(ValidationError):
        ToolResult(id="call", ok=True)


def test_failed_tool_result_round_trips_error_code() -> None:
    message = ToolResult(
        id="call",
        ok=False,
        error=ErrorDetail(code=ErrorCode.NOT_ALLOWED, message="denied"),
    )
    decoded = parse_message(json.loads(encode_message(message)))
    assert isinstance(decoded, ToolResult)
    assert decoded.error is not None
    assert decoded.error.code == ErrorCode.NOT_ALLOWED


def test_parse_rejects_binary_and_unknown_messages() -> None:
    with pytest.raises(ProtocolError, match="binary"):
        parse_message(b"{}")
    with pytest.raises(ProtocolError, match="unknown message"):
        parse_message({"type": "unknown", "v": 1})


def test_tool_call_forbids_extra_wire_fields() -> None:
    with pytest.raises(ProtocolError):
        parse_message(
            {
                "type": "tool_call",
                "v": 1,
                "id": "id",
                "trace_id": "trace",
                "tool": "get_pods",
                "args": {},
                "deadline_ms": 100,
                "cluster": "not-on-wire",
            }
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"type": [], "v": 1},
        {"type": {}, "v": 1},
        {"type": "hello", "v": True},
        {"type": "hello", "v": [1]},
        {"type": "hello", "v": 1, "cluster_id": "private-input"},
    ],
)
def test_malformed_frames_fail_without_exposing_payload(payload: dict[str, object]) -> None:
    with pytest.raises(ProtocolError) as caught:
        parse_message(payload)
    assert "private-input" not in str(caught.value)


def test_failed_result_rejects_attached_payload() -> None:
    with pytest.raises(ValidationError):
        ToolResult(
            id="call",
            ok=False,
            data={"private": "must not cross the error boundary"},
            error=ErrorDetail(code=ErrorCode.UPSTREAM_ERROR, message="lookup failed"),
        )


def test_same_version_optional_metadata_remains_compatible() -> None:
    decoded = parse_message(
        {
            "type": "hello",
            "v": 1,
            "cluster_id": "test-cluster",
            "spoke_version": "0.1.0",
            "future_metadata": {"optional": True},
        }
    )
    assert isinstance(decoded, Hello)
    assert decoded.cluster_id == "test-cluster"
