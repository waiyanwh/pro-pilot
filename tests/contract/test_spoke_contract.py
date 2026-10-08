import pytest

from pro_pilot.protocol.messages import ErrorCode
from pro_pilot.protocol.tools import TOOL_REGISTRY
from pro_pilot.spoke.pipeline import ToolPipeline
from pro_pilot.spoke.policy import NamespacePolicy


class UnexpectedExecutor:
    async def execute(self, tool_name: str, args: dict[str, object]) -> object:
        raise AssertionError(f"denied call reached executor: {tool_name}")


def denied_args(tool_name: str) -> dict[str, object]:
    args: dict[str, object] = {"namespace": "kube-system"}
    if tool_name in {"get_workload"}:
        args.update({"kind": "deployment", "name": "api"})
    elif tool_name in {"describe_pod"}:
        args["name"] = "api-0"
    elif tool_name in {"get_pod_logs"}:
        args["pod"] = "api-0"
    elif tool_name in {"get_rollout"}:
        args["name"] = "api"
    elif tool_name in {"list_analysis_runs"}:
        pass
    elif tool_name in {"get_alb_for_ingress", "get_alb_metrics"}:
        args["ingress"] = "api"
        if tool_name == "get_alb_metrics":
            args["metrics"] = ["requests"]
    return args


@pytest.mark.asyncio
async def test_every_namespaced_tool_rejects_denied_namespace_before_execution() -> None:
    namespaced = [name for name, definition in TOOL_REGISTRY.items() if definition.namespaced]
    pipeline = ToolPipeline(
        UnexpectedExecutor(),
        NamespacePolicy(("*",), ("kube-*",)),
        advertised_tools=set(namespaced),
    )
    for tool_name in namespaced:
        result = await pipeline.execute(
            f"call-{tool_name}",
            tool_name,
            denied_args(tool_name),
            deadline_ms=1000,
        )
        assert not result.ok, tool_name
        assert result.error is not None
        assert result.error.code == ErrorCode.NOT_ALLOWED, tool_name
