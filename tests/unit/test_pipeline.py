import asyncio
import json
from pathlib import Path

import pytest

from pro_pilot.protocol.messages import ErrorCode, ToolResult
from pro_pilot.spoke.fake import FakeFixtureExecutor
from pro_pilot.spoke.pipeline import PipelineConfig, ToolPipeline, _cap_payload
from pro_pilot.spoke.policy import NamespacePolicy


@pytest.fixture
def fixture_root() -> str:
    return "tests/fixtures/clusters/demo-cluster"


@pytest.mark.asyncio
async def test_fake_fixture_runs_full_pipeline_and_redacts(fixture_root: str) -> None:
    pipeline = ToolPipeline(
        FakeFixtureExecutor(fixture_root, "logs-with-secrets"),
        NamespacePolicy(("*",), ()),
        advertised_tools={"get_pod_logs"},
    )
    result = await pipeline.execute(
        "call-1",
        "get_pod_logs",
        {"namespace": "payment", "pod": "api"},
        deadline_ms=1000,
    )
    assert result.ok
    assert result.data is not None
    assert "hunter2" not in str(result.data)
    assert "eyJhbGci" not in str(result.data)
    assert result.redactions >= 3


@pytest.mark.asyncio
async def test_denied_namespace_is_rejected_before_executor() -> None:
    class FailingExecutor:
        async def execute(self, tool_name: str, args: dict[str, object]) -> object:
            raise AssertionError("executor should not be called")

    pipeline = ToolPipeline(
        FailingExecutor(),
        NamespacePolicy(("*",), ("kube-*",)),
        advertised_tools={"get_pods"},
    )
    result = await pipeline.execute(
        "call-2",
        "get_pods",
        {"namespace": "kube-system"},
        deadline_ms=1000,
    )
    assert not result.ok
    assert result.error is not None
    assert result.error.code == ErrorCode.NOT_ALLOWED


@pytest.mark.asyncio
async def test_extra_args_fail_validation() -> None:
    pipeline = ToolPipeline(
        FakeFixtureExecutor("tests/fixtures/clusters/demo-cluster", "crashloop-oom"),
        NamespacePolicy(("*",), ()),
        advertised_tools={"get_pods"},
    )
    result = await pipeline.execute(
        "call-3",
        "get_pods",
        {"namespace": "payment", "unexpected": True},
        deadline_ms=1000,
    )
    assert not result.ok
    assert result.error is not None
    assert result.error.code == ErrorCode.INVALID_ARGS


@pytest.mark.asyncio
async def test_cap_keeps_last_log_lines() -> None:
    class Executor:
        async def execute(self, tool_name: str, args: dict[str, object]) -> object:
            return {"container": "api", "lines": ["old-" * 100, "middle-" * 100, "last-line"]}

    pipeline = ToolPipeline(
        Executor(),
        NamespacePolicy(("*",), ()),
        advertised_tools={"get_pod_logs"},
        config=PipelineConfig(max_result_bytes=120),
    )
    result = await pipeline.execute(
        "call-4",
        "get_pod_logs",
        {"namespace": "payment", "pod": "api"},
        deadline_ms=1000,
    )
    assert result.ok
    assert result.truncated
    assert isinstance(result.data, dict)
    assert result.data["lines"][-1] == "last-line"


@pytest.mark.asyncio
async def test_namespace_filter_runs_before_projection_for_cluster_wide_lists() -> None:
    """Regression: a denied item that would also fail output-model validation
    (missing the required ``name`` field) must be dropped by namespace policy
    before projection ever sees it, per spec/spoke.md step 3. If filtering
    ran after projection instead, this call would fail the whole list with a
    validation error rather than silently dropping the denied item."""

    class Executor:
        async def execute(self, tool_name: str, args: dict[str, object]) -> object:
            return [
                {
                    "name": "payment-app",
                    "destination_namespace": "payment",
                    "project": "default",
                    "sync": "Synced",
                    "health": "Healthy",
                },
                {"destination_namespace": "kube-system"},  # denied and missing "name"
            ]

    pipeline = ToolPipeline(
        Executor(),
        NamespacePolicy(("*",), ("kube-*",)),
        advertised_tools={"list_applications"},
    )
    result = await pipeline.execute("call-6", "list_applications", {}, deadline_ms=1000)
    assert result.ok, result.error
    assert isinstance(result.data, list)
    assert len(result.data) == 1
    assert result.data[0]["name"] == "payment-app"


@pytest.mark.asyncio
async def test_pod_logs_contains_filters_lines_case_insensitively() -> None:
    class Executor:
        async def execute(self, tool_name: str, args: dict[str, object]) -> object:
            return {
                "container": "api",
                "lines": ["INFO boot", "WARN Disk low", "INFO boot", "ERROR disk failure"],
                "truncated": False,
            }

    pipeline = ToolPipeline(
        Executor(), NamespacePolicy(("*",), ()), advertised_tools={"get_pod_logs"}
    )
    result = await pipeline.execute(
        "call-7",
        "get_pod_logs",
        {"namespace": "payment", "pod": "p", "contains": "disk"},
        deadline_ms=1000,
    )
    assert result.ok
    assert isinstance(result.data, dict)
    assert result.data["lines"] == ["WARN Disk low", "ERROR disk failure"]


@pytest.mark.asyncio
async def test_pod_logs_tail_lines_keeps_last_n_and_marks_truncated() -> None:
    class Executor:
        async def execute(self, tool_name: str, args: dict[str, object]) -> object:
            return {
                "container": "api",
                "lines": ["one", "two", "three", "four"],
                "truncated": False,
            }

    pipeline = ToolPipeline(
        Executor(), NamespacePolicy(("*",), ()), advertised_tools={"get_pod_logs"}
    )
    result = await pipeline.execute(
        "call-8",
        "get_pod_logs",
        {"namespace": "payment", "pod": "p", "tail_lines": 2},
        deadline_ms=1000,
    )
    assert result.ok
    assert isinstance(result.data, dict)
    assert result.data["lines"] == ["three", "four"]
    assert result.data["truncated"] is True


@pytest.mark.asyncio
async def test_pod_log_line_cap_applies_after_redaction_not_before() -> None:
    """Regression: a secret that straddles the 1,000-char line cap must be
    fully redacted first; truncating the raw line first would leave half a
    secret, unredacted, in the output."""

    secret = "password=" + ("x" * 50)
    padding = "a" * 1200 + " "
    long_line = padding + secret

    class Executor:
        async def execute(self, tool_name: str, args: dict[str, object]) -> object:
            return {"container": "api", "lines": [long_line], "truncated": False}

    pipeline = ToolPipeline(
        Executor(), NamespacePolicy(("*",), ()), advertised_tools={"get_pod_logs"}
    )
    result = await pipeline.execute(
        "call-9", "get_pod_logs", {"namespace": "payment", "pod": "p"}, deadline_ms=1000
    )
    assert result.ok
    assert isinstance(result.data, dict)
    line = result.data["lines"][0]
    assert len(line) <= 1000
    assert "xxxxx" not in line
    assert result.data["truncated"] is True


@pytest.mark.asyncio
async def test_total_deadline_includes_queue_wait_time() -> None:
    """Regression: the hard per-tool deadline (``deadline_ms``) must cover
    time spent waiting for a free ``max_concurrent_tools`` slot, not just the
    execution phase. A call that is queued for most of its deadline must not
    be granted a fresh full timeout once it starts executing."""

    class SlowFirstExecutor:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, tool_name: str, args: dict[str, object]) -> object:
            self.calls += 1
            if self.calls == 1:
                await asyncio.sleep(0.15)
                return []
            await asyncio.sleep(0.1)
            return []

    pipeline = ToolPipeline(
        SlowFirstExecutor(),
        NamespacePolicy(("*",), ()),
        advertised_tools={"get_pods"},
        config=PipelineConfig(tool_timeout_s=1.0, max_concurrent_tools=1),
    )

    async def call(call_id: str) -> ToolResult:
        return await pipeline.execute(
            call_id, "get_pods", {"namespace": "payment"}, deadline_ms=200
        )

    first_task = asyncio.create_task(call("first"))
    await asyncio.sleep(0.01)
    second_task = asyncio.create_task(call("second"))
    first_result, second_result = await asyncio.gather(first_task, second_task)
    assert first_result.ok
    assert not second_result.ok
    assert second_result.error is not None
    assert second_result.error.code == ErrorCode.TIMEOUT


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 2])
async def test_impossibly_small_byte_cap_terminates_without_oversized_success(limit: int) -> None:
    """Regression/edge case: a `max_result_bytes` cap too small to ever hold
    a valid JSON document (e.g. misconfigured to 1-2 bytes) must not hang,
    infinite-loop, or return `ok=True` with an oversized/corrupt payload.
    `_cap_payload` must give up and the pipeline must report a bounded
    failure instead."""

    class Executor:
        async def execute(self, tool_name: str, args: dict[str, object]) -> object:
            return [{"name": "pod-name", "phase": "Running"}]

    pipeline = ToolPipeline(
        Executor(),
        NamespacePolicy(("payment",), ()),
        advertised_tools={"get_pods"},
        config=PipelineConfig(max_result_bytes=limit),
    )
    result = await asyncio.wait_for(
        pipeline.execute("call-12", "get_pods", {"namespace": "payment"}, deadline_ms=1000),
        timeout=0.5,
    )
    if result.ok:
        assert len(json.dumps(result.data, separators=(",", ":")).encode()) <= limit
        assert result.truncated
    else:
        assert result.error is not None


def test_cap_payload_shrinks_scalar_fields_without_corrupting_json() -> None:
    """Regression: when there are no list items left to drop (a purely
    scalar/oversized-string payload), the cap must shrink the offending
    string field in place and stay a valid JSON object - never hand-slice
    the already-escaped JSON bytes of the whole document, which corrupts
    multi-byte or quoted content and can return a bare string instead of the
    tool's object/array shape."""

    payload = {"name": "identity", "message": ('héllo "wörld" ' * 200)}
    capped, truncated = _cap_payload(payload, 200)
    assert truncated
    assert isinstance(capped, dict)
    encoded = json.dumps(capped, ensure_ascii=False)
    assert len(encoded.encode()) <= 200
    json.loads(encoded)  # must still be valid, well-formed JSON


_FIXTURE_ROOT = Path("tests/fixtures/clusters/demo-cluster")
_DEFAULT_ARGS: dict[str, dict[str, object]] = {
    "list_namespaces": {},
    "get_pods": {"namespace": "payment"},
    "describe_pod": {"namespace": "payment", "name": "pod-a"},
    "get_pod_logs": {"namespace": "payment", "pod": "pod-a"},
    "get_workload": {"namespace": "payment", "kind": "deployment", "name": "wl"},
    "top_pods": {"namespace": "payment"},
    "get_events": {"namespace": "payment"},
    "list_nodeclaims": {},
    "list_applications": {},
    "get_application": {"name": "identity"},
    "get_rollout": {"namespace": "checkout", "name": "checkout"},
    "list_analysis_runs": {"namespace": "checkout"},
}


def _fixture_scenarios() -> list[str]:
    return sorted(p.name for p in _FIXTURE_ROOT.iterdir() if p.is_dir())


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", _fixture_scenarios())
async def test_every_fixture_scenario_passes_the_full_pipeline_without_leaks(
    scenario: str,
) -> None:
    """Audit all six Phase 0 fixtures end to end: every tool a scenario
    advertises must validate, pass namespace policy, project, and redact
    cleanly with plausible args, and the raw secrets embedded in
    `logs-with-secrets` / `argo-app-outofsync` must never reach the result."""

    executor = FakeFixtureExecutor(str(_FIXTURE_ROOT), scenario)
    pipeline = ToolPipeline(
        executor,
        NamespacePolicy(("*",), ()),
        advertised_tools=set(executor.advertised_tools()),
    )
    for tool_name in executor.advertised_tools():
        args = dict(_DEFAULT_ARGS.get(tool_name, {}))
        result = await pipeline.execute(
            f"{scenario}-{tool_name}", tool_name, args, deadline_ms=5000
        )
        assert result.ok, (scenario, tool_name, result.error)
        payload = json.dumps(result.data)
        assert "hunter2" not in payload
        assert "eyJhbGci" not in payload
        assert "4111 1111 1111 1111" not in payload


@pytest.mark.asyncio
async def test_prompt_injection_fixture_passes_through_as_inert_untrusted_data() -> None:
    """The spoke has no business sanitizing prompt-injection attempts in log
    text (that is a hub/prompt concern per docs/security.md T4); it must
    only apply real secret redaction, and only to the one line that happens
    to carry a credential-shaped query string (`?token=abc`). The injection
    sentence itself must reach the hub byte-for-byte so the hub can wrap it
    as untrusted data."""

    executor = FakeFixtureExecutor(str(_FIXTURE_ROOT), "logs-with-prompt-injection")
    pipeline = ToolPipeline(
        executor, NamespacePolicy(("*",), ()), advertised_tools={"get_pod_logs"}
    )
    result = await pipeline.execute(
        "call-10",
        "get_pod_logs",
        {"namespace": "payment", "pod": "pod-a"},
        deadline_ms=1000,
    )
    assert result.ok
    assert isinstance(result.data, dict)
    assert result.redactions == 1  # only the "?token=abc" query string
    assert any("ignore previous instructions" in line for line in result.data["lines"])
    assert any(line == "INFO worker healthy" for line in result.data["lines"])


@pytest.mark.asyncio
async def test_max_list_items_caps_cluster_wide_lists_before_projection() -> None:
    class Executor:
        async def execute(self, tool_name: str, args: dict[str, object]) -> object:
            return [{"name": f"ns-{i}", "phase": "Active"} for i in range(10)]

    pipeline = ToolPipeline(
        Executor(),
        NamespacePolicy(("*",), ()),
        advertised_tools={"list_namespaces"},
        config=PipelineConfig(max_list_items=3),
    )
    result = await pipeline.execute("call-11", "list_namespaces", {}, deadline_ms=1000)
    assert result.ok
    assert isinstance(result.data, list)
    assert len(result.data) == 3


@pytest.mark.asyncio
async def test_timeout_maps_to_timeout_error() -> None:
    class SlowExecutor:
        async def execute(self, tool_name: str, args: dict[str, object]) -> object:
            await asyncio.sleep(0.05)
            return []

    pipeline = ToolPipeline(
        SlowExecutor(),
        NamespacePolicy(("*",), ()),
        advertised_tools={"get_pods"},
        config=PipelineConfig(tool_timeout_s=0.01),
    )
    result = await pipeline.execute(
        "call-5",
        "get_pods",
        {"namespace": "payment"},
        deadline_ms=1000,
    )
    assert not result.ok
    assert result.error is not None
    assert result.error.code == ErrorCode.TIMEOUT
