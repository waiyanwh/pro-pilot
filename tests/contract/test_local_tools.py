from __future__ import annotations

import ast
import copy
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from pro_pilot.protocol.messages import ErrorCode
from pro_pilot.protocol.tools import TOOL_REGISTRY
from pro_pilot.spoke.execution import ExecutionResult
from pro_pilot.spoke.pipeline import PipelineConfig, ToolPipeline
from tests.local_support import load_fixture, local_executor

SPOKE_TOOLS = [name for name, tool in TOOL_REGISTRY.items() if not tool.hub_local]
CLUSTER_TOOLS = {
    "list_namespaces",
    "get_nodes",
    "list_nodeclaims",
    "get_eks_cluster",
    "list_nodegroups",
}


@pytest.fixture(autouse=True)
def fixture_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed = datetime(2026, 10, 5, 12, tzinfo=UTC)
    monkeypatch.setattr("pro_pilot.spoke.tools.k8s.utc_now", lambda: fixed)


def tool_fixture(tool: str) -> dict[str, Any]:
    group = TOOL_REGISTRY[tool].capability
    return load_fixture(
        "k8s"
        if group in {"k8s", "metrics", "karpenter"}
        else "argo"
        if group in {"argocd", "rollouts"}
        else "aws"
    )


def add_unknown_raw_fields(value: Any) -> Any:
    result = copy.deepcopy(value)
    if isinstance(result, dict):
        for key, child in list(result.items()):
            result[key] = add_unknown_raw_fields(child)
        metadata = result.get("metadata")
        if isinstance(metadata, dict):
            result["unexpected_private_field"] = "unredacted-private-canary"
            metadata["managedFields"] = [{"private": "unredacted-private-canary"}]
        elif set(result) & {
            "cluster",
            "addon",
            "nodegroup",
            "LoadBalancers",
            "TargetGroups",
            "TargetHealthDescriptions",
            "MetricDataResults",
        }:
            result["unexpected_private_field"] = "unredacted-private-canary"
    elif isinstance(result, list):
        result = [add_unknown_raw_fields(item) for item in result]
    return result


def replace_first_string(value: Any, replacement: str) -> bool:
    if isinstance(value, dict):
        for key in ("phase", "message", "age", "image"):
            if isinstance(value.get(key), str):
                value[key] = replacement
                return True
        for key, child in value.items():
            if isinstance(child, str):
                value[key] = replacement
                return True
            if replace_first_string(child, replacement):
                return True
    elif isinstance(value, list):
        for child in value:
            if replace_first_string(child, replacement):
                return True
    return False


@pytest.mark.parametrize("tool", SPOKE_TOOLS)
async def test_local_tool_rejects_invalid_args_before_read(tool: str) -> None:
    fixture = tool_fixture(tool)
    executor = local_executor(fixture)
    try:
        pipeline = ToolPipeline(executor, executor.policy)
        args = {**fixture["args"][tool], "unexpected": "value"}
        result = await pipeline.execute("invalid", tool, args, deadline_ms=1000)
        assert result.error is not None and result.error.code == ErrorCode.INVALID_ARGS
        assert executor.reader.api_client.calls == []
        assert executor.sts.calls == []
    finally:
        await executor.close()


@pytest.mark.parametrize("tool", SPOKE_TOOLS)
async def test_local_tool_preserves_declared_scope(tool: str) -> None:
    fixture = tool_fixture(tool)
    args = copy.deepcopy(fixture["args"][tool])
    if TOOL_REGISTRY[tool].namespaced:
        args["namespace"] = "kube-system"
    elif tool in {"list_applications", "get_application"}:
        for path, resource in fixture["routes"].items():
            if "/applications" not in path:
                continue
            apps = resource.get("items", [resource])
            for app in apps:
                app.setdefault("spec", {}).setdefault("destination", {})["namespace"] = (
                    "kube-system"
                )
    else:
        args["namespace"] = "kube-system"
    executor = local_executor(fixture)
    try:
        result = await ToolPipeline(executor, executor.policy).execute(
            "denied", tool, args, deadline_ms=1000
        )
        if TOOL_REGISTRY[tool].namespaced or tool == "get_application":
            assert result.error is not None and result.error.code == ErrorCode.NOT_ALLOWED
        elif tool == "list_applications":
            assert result.ok and result.data == []
        else:
            assert tool in CLUSTER_TOOLS
            assert result.error is not None and result.error.code == ErrorCode.INVALID_ARGS
    finally:
        await executor.close()


@pytest.mark.parametrize("tool", SPOKE_TOOLS)
async def test_local_tool_projects_raw_objects_without_private_extras(tool: str) -> None:
    fixture = tool_fixture(tool)
    fixture["routes"] = add_unknown_raw_fields(fixture["routes"])
    fixture["aws"] = add_unknown_raw_fields(fixture.get("aws", {}))
    executor = local_executor(fixture)
    try:
        result = await ToolPipeline(executor, executor.policy).execute(
            "project",
            tool,
            fixture["args"][tool],
            deadline_ms=2000,
        )
        assert result.ok, result.error
        encoded = json.dumps(result.data)
        assert "unredacted-private-canary" not in encoded
        assert "managedFields" not in encoded
        assert '"valuesObject"' not in encoded
        assert '"unexpected_private_field"' not in encoded
    finally:
        await executor.close()


class EgressProbe:
    def __init__(self, executor: Any, replacement: str) -> None:
        self.executor = executor
        self.replacement = replacement

    async def execute(self, tool_name: str, args: dict[str, Any]) -> ExecutionResult:
        result = await self.executor.execute(tool_name, args)
        data = copy.deepcopy(result.data)
        assert replace_first_string(data, self.replacement), "fixture must carry a projected string"
        return ExecutionResult(data, result.truncated)


@pytest.mark.parametrize("tool", SPOKE_TOOLS)
async def test_local_tool_redacts_before_egress(tool: str) -> None:
    fixture = tool_fixture(tool)
    executor = local_executor(fixture)
    try:
        pipeline = ToolPipeline(EgressProbe(executor, "password=private-value"), executor.policy)
        result = await pipeline.execute("redact", tool, fixture["args"][tool], deadline_ms=2000)
        assert result.ok, result.error
        assert result.redactions >= 1
        assert "private-value" not in json.dumps(result.data)
        assert "[REDACTED:" in json.dumps(result.data)
    finally:
        await executor.close()


@pytest.mark.parametrize("tool", SPOKE_TOOLS)
async def test_local_tool_bounds_serialized_result(tool: str) -> None:
    fixture = tool_fixture(tool)
    executor = local_executor(fixture)
    try:
        pipeline = ToolPipeline(
            EgressProbe(executor, 'é"\\' * 12000),
            executor.policy,
            config=PipelineConfig(max_result_bytes=600),
        )
        result = await pipeline.execute("size", tool, fixture["args"][tool], deadline_ms=2000)
        assert result.ok, result.error
        assert result.truncated
        assert (
            len(json.dumps(result.data, ensure_ascii=False, separators=(",", ":")).encode()) <= 600
        )
    finally:
        await executor.close()


_MUTATOR = re.compile(
    r"^(create|delete|patch|replace|update|put|start|stop|invoke|terminate|reboot|modify|attach|detach|associate|disassociate|scale|sync|promote|abort|impersonate)_"
)


def mutation_references(source: str) -> list[str]:
    issues = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "asyncio"
        ):
            continue
        if isinstance(node, ast.Attribute) and (
            _MUTATOR.match(node.attr)
            or node.attr in {"exec", "attach", "portforward", "proxy", "impersonate"}
        ):
            issues.append(f"line {node.lineno}: {node.attr}")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "call_api"
            and (
                len(node.args) < 2
                or not isinstance(node.args[1], ast.Constant)
                or node.args[1].value != "GET"
            )
        ):
            issues.append(f"line {node.lineno}: non-literal GET request")
    return issues


def test_spoke_source_has_no_mutating_client_references() -> None:
    root = Path(__file__).parents[2] / "pro_pilot" / "spoke"
    failures = {
        str(path.relative_to(root)): issues
        for path in root.rglob("*.py")
        if (issues := mutation_references(path.read_text()))
    }
    assert failures == {}


def test_mutation_guard_rejects_sdk_reference_and_non_get_transport() -> None:
    assert mutation_references("client.delete_namespaced_pod")
    assert mutation_references("eks.update_nodegroup_config")
    assert mutation_references("api.call_api('/api/v1/pods', 'POST')")
    assert mutation_references("api.call_api(path, method)")
