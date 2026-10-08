import json

import pytest
from pydantic import ValidationError

from pro_pilot.protocol.tools import (
    TOOL_REGISTRY,
    GetNodesArgs,
    GetPodsArgs,
    bedrock_tool_specs,
    tool_schema,
)


def test_registry_contains_every_catalogue_tool() -> None:
    assert len(TOOL_REGISTRY) == 22
    assert set(TOOL_REGISTRY) == {
        "list_clusters",
        "list_namespaces",
        "list_workloads",
        "get_workload",
        "get_pods",
        "describe_pod",
        "get_events",
        "get_pod_logs",
        "list_services",
        "list_ingresses",
        "list_hpas",
        "get_nodes",
        "top_pods",
        "list_nodeclaims",
        "list_applications",
        "get_application",
        "get_rollout",
        "list_analysis_runs",
        "get_eks_cluster",
        "list_nodegroups",
        "get_alb_for_ingress",
        "get_alb_metrics",
    }


def test_every_registry_entry_exports_a_valid_json_schema() -> None:
    for definition in TOOL_REGISTRY.values():
        schema = tool_schema(definition, ["demo-cluster"])
        json.dumps(schema)
        assert schema["type"] == "object"
        assert "properties" in schema
        if not definition.hub_local:
            assert schema["properties"]["cluster"]["enum"] == ["demo-cluster"]
            assert "cluster" in schema["required"]


def test_bedrock_tool_spec_shape() -> None:
    specs = bedrock_tool_specs(["get_pods"], ["staging"])
    assert specs[0]["toolSpec"]["name"] == "get_pods"
    assert specs[0]["toolSpec"]["inputSchema"]["json"]["properties"]["cluster"]


def test_args_models_forbid_extra_fields() -> None:
    for definition in TOOL_REGISTRY.values():
        with pytest.raises(ValidationError):
            definition.args_model.model_validate({"unexpected_field": "x"})


@pytest.mark.parametrize(
    "selector",
    [
        "env=prod",
        "env==prod",
        "env!=prod",
        "tier",
        "!legacy",
        "env=prod,tier!=db",
        "env in (prod, staging)",
        "env notin (prod,staging)",
        "app.kubernetes.io/name=api",
    ],
)
def test_label_selector_accepts_valid_kubernetes_grammar(selector: str) -> None:
    GetPodsArgs(namespace="payment", label_selector=selector)
    GetNodesArgs(label_selector=selector)


@pytest.mark.parametrize(
    "selector",
    [
        "env==,,",
        "env in (",
        "env in )",
        "a,,b",
        "env===prod",
        "=prod",
        "env=prod)",
        "(env=prod",
        "   ",
        "env in prod",
    ],
)
def test_label_selector_rejects_syntactically_invalid_values(selector: str) -> None:
    """Regression: the permissive character-class check alone lets malformed
    selectors like unbalanced parens or doubled operators through. The spoke
    must also parse-check against the Kubernetes requirement grammar
    (spec/tools.md #4)."""

    with pytest.raises(ValidationError):
        GetPodsArgs(namespace="payment", label_selector=selector)
    with pytest.raises(ValidationError):
        GetNodesArgs(label_selector=selector)
