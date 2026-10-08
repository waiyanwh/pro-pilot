"""Offline behaviour tests using raw Kubernetes HTTP response fixtures."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from pro_pilot.protocol.tools import TOOL_REGISTRY
from pro_pilot.spoke.execution import TRUNCATED
from pro_pilot.spoke.policy import NamespacePolicy
from pro_pilot.spoke.tools import k8s
from pro_pilot.spoke.tools.common import KubeReader
from pro_pilot.spoke.tools.k8s import K8sTools
from tests.local_support import FixtureApi, load_fixture


@pytest.fixture
def harness(monkeypatch):
    payload = load_fixture("k8s")
    api = FixtureApi(payload["routes"])
    tools = K8sTools(KubeReader(api), NamespacePolicy())
    monkeypatch.setattr(k8s, "utc_now", lambda: datetime(2026, 10, 5, 12, tzinfo=UTC))
    token = TRUNCATED.set(False)
    yield tools, api, payload["args"]
    TRUNCATED.reset(token)


@pytest.mark.parametrize(
    "tool",
    [
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
    ],
)
def test_raw_fixtures_produce_catalog_outputs(harness, tool):
    tools, _, args = harness
    validated = TOOL_REGISTRY[tool].args_model.model_validate(args[tool]).model_dump()
    result = tools.execute(tool, validated)
    # The fixture matrix must produce meaningful, nonempty results, not placeholders.
    assert result
    TOOL_REGISTRY[tool].output_model.model_validate(result)
    assert "never-return-this" not in json.dumps(result)


def test_workload_default_kinds_and_replica_history(harness):
    tools, api, args = harness
    workloads = tools.execute("list_workloads", args["list_workloads"])
    assert {w["kind"] for w in workloads} == {"deployment", "statefulset", "daemonset", "cronjob"}
    assert next(w for w in workloads if w["kind"] == "deployment")["problem"] == "one unavailable"
    assert not any(path.endswith("/jobs") for path, _ in api.calls)
    detail = tools.execute("get_workload", args["get_workload"])
    assert detail["replicas"] == {"desired": 2, "ready": 1, "updated": 2, "available": 1}
    assert detail["current_replicaset"] == "api-2"
    assert detail["previous_replicaset"] == {"name": "api-1", "revision": "1", "images": ["api:v1"]}
    assert detail["hpa"] == {"min": 2, "max": 5, "current": 2, "desired": 3}
    assert detail["containers"][0]["probes"]["liveness"] == {"type": "exec"}


def test_pod_ownership_restarts_and_selector_intersection(harness):
    tools, api, args = harness
    result = tools.execute("get_pods", {**args["get_pods"], "label_selector": "tier=web"})
    assert [p["name"] for p in result] == ["api-2-a"]
    assert result[0]["restarts"] == 4
    assert result[0]["ready"] == "0/1"
    assert result[0]["last_termination"]["exit_code"] == 137
    assert result[0]["waiting_reason"] == "CrashLoopBackOff"
    assert any(
        query.get("labelSelector") == "app=api,tier=web"
        for path, query in api.calls
        if path.endswith("/pods")
    )


@pytest.mark.parametrize(
    "kind,name,expected",
    [("job", "nightly-1", "nightly-pod"), ("cronjob", "nightly", "nightly-pod")],
)
def test_batch_workload_references_follow_owned_jobs(harness, kind, name, expected):
    tools, _, _ = harness
    result = tools.execute(
        "get_pods", {"namespace": "payments", "workload": {"kind": kind, "name": name}}
    )
    assert [p["name"] for p in result] == [expected]


def test_missing_selector_never_lists_pods(harness):
    tools, api, args = harness
    del api.routes["/apis/apps/v1/namespaces/payments/deployments/api"]["spec"]["selector"]
    assert tools.execute("get_pods", args["get_pods"]) == []
    assert tools.execute("top_pods", args["top_pods"]) == []
    assert not any(path.endswith("/pods") for path, _ in api.calls)


def test_missing_job_selector_does_not_broaden_cronjob(harness):
    tools, api, _ = harness
    del api.routes["/apis/batch/v1/namespaces/payments/jobs"]["items"][0]["spec"]["selector"]
    assert (
        tools.execute(
            "get_pods",
            {"namespace": "payments", "workload": {"kind": "cronjob", "name": "nightly"}},
        )
        == []
    )
    assert not any(path.endswith("/pods") for path, _ in api.calls)


@pytest.mark.parametrize(
    "status,expected",
    [
        ("not_ready", ["api-2-a", "nightly-pod"]),
        ("pending", []),
        ("restarting", ["api-2-a", "unrelated", "nightly-pod"]),
    ],
)
def test_pod_status_filters(harness, status, expected):
    tools, _, _ = harness
    assert [
        p["name"] for p in tools.execute("get_pods", {"namespace": "payments", "status": status})
    ] == expected


def test_description_projects_env_probes_states_and_pod_events(harness):
    tools, _, args = harness
    result = tools.execute("describe_pod", args["describe_pod"])
    container = result["containers"][0]
    assert container["env"] == [
        {"name": "PASSWORD"},
        {"name": "TOKEN", "value_from": {"secretKeyRef": {"name": "api-token", "key": "token"}}},
    ]
    assert container["probes"]["readiness"] == {
        "type": "http",
        "path": "/health",
        "port": "8080",
        "period_seconds": 5,
    }
    assert container["last_state"]["terminated"]["exit_code"] == 137
    assert result["init_containers"][0]["env"] == [{"name": "INIT_SECRET"}]
    assert len(result["events"]) == 3
    assert all(e["object"] == "Pod/api-2-a" for e in result["events"])


def test_events_apply_time_type_and_object_filters(harness):
    tools, api, args = harness
    result = tools.execute("get_events", args["get_events"])
    assert len(result) == 1
    assert result[0]["last_seen"] == "2026-10-05T11:59:00Z"
    query = api.calls[-1][1]["fieldSelector"]
    assert "type=Warning" in query and "involvedObject.name=api-2-a" in query
    all_events = tools.execute(
        "get_events", {"namespace": "payments", "type": "all", "since_minutes": 180}
    )
    assert [e["last_seen"] for e in all_events] == sorted(
        [e["last_seen"] for e in all_events], reverse=True
    )


def test_logs_request_previous_and_bound_tail_and_since(harness):
    _, api, args = harness
    tools = K8sTools(KubeReader(api), NamespacePolicy(), max_log_lines=2, max_log_since_s=60)
    result = tools.execute("get_pod_logs", args["get_pod_logs"])
    assert result == {
        "container": "api",
        "lines": ["ERROR out of memory", "last error"],
        "truncated": True,
    }
    assert TRUNCATED.get()
    assert api.calls[-1] == (
        "/api/v1/namespaces/payments/pods/api-2-a/log",
        {
            "container": "api",
            "tailLines": 2,
            "sinceSeconds": 60,
            "previous": True,
            "timestamps": False,
        },
    )


def test_multicontainer_logs_refuse_ambiguity_and_unknown_container(harness):
    tools, api, args = harness
    api.routes["/api/v1/namespaces/payments/pods/api-2-a"]["spec"]["containers"].append(
        {"name": "sidecar"}
    )
    with pytest.raises(ValueError, match="must be specified"):
        tools.execute("get_pod_logs", args["get_pod_logs"])
    with pytest.raises(ValueError, match="does not exist"):
        tools.execute("get_pod_logs", {**args["get_pod_logs"], "container": "missing"})
    assert not any(path.endswith("/log") for path, _ in api.calls)
    result = tools.execute("get_pod_logs", {**args["get_pod_logs"], "container": "init"})
    assert result["container"] == "init"


def test_service_endpoint_counts_and_ingress_allowlist(harness):
    tools, _, args = harness
    service = tools.execute("list_services", args["list_services"])[0]
    assert service["ready_endpoints"] == 1 and service["not_ready_endpoints"] == 1
    assert service["ports"][0]["targetPort"] == 8080
    ingress = tools.execute("list_ingresses", args["list_ingresses"])[0]
    assert ingress["rules"] == [
        {"host": "api.example.test", "path": "/", "service": "api", "port": 80}
    ]
    assert len(ingress["annotations"]) == 3
    assert "unknown" not in ingress["annotations"]
    assert ingress["tls_hosts"] == ["api.example.test"]


def test_hpa_resource_and_container_resource_metrics(harness):
    tools, _, args = harness
    result = tools.execute("list_hpas", args["list_hpas"])[0]
    assert result["target"] == "Deployment/api"
    assert result["metrics"] == [
        {"name": "cpu", "current": 85, "target": 70},
        {"name": "memory", "current": "300Mi", "target": "256Mi"},
    ]


def test_node_pressure_and_cluster_scoping(harness):
    tools, api, args = harness
    result = tools.execute("get_nodes", args["get_nodes"])[0]
    assert result["ready"] is False and result["pressure"] == ["Memory"]
    assert result["taints"] == [{"key": "dedicated", "effect": "NoSchedule"}]
    assert set(result["capacity"]) == {"cpu", "memory"}
    assert api.calls[-1][0] == "/api/v1/nodes"
    assert [r["name"] for r in tools.execute("list_namespaces", {})] == ["payments"]


def test_metrics_quantity_conversion_requests_and_sorting(harness):
    tools, _, args = harness
    result = tools.execute("top_pods", args["top_pods"])
    assert len(result) == 1
    assert result[0] == {
        "pod": "api-2-a",
        "container": "api",
        "cpu_millicores": 250.0,
        "memory_mib": 300.0,
        "cpu_request": "100m",
        "memory_request": "128Mi",
        "memory_limit": "512Mi",
    }
    unfiltered = tools.execute("top_pods", {"namespace": "payments"})
    assert [r["memory_mib"] for r in unfiltered] == [300.0, 20.0]


def test_nodeclaim_owner_pool_launch_time_and_nontrue_conditions(harness):
    tools, api, args = harness
    result = tools.execute("list_nodeclaims", args["list_nodeclaims"])[0]
    assert result["nodepool"] == "apps"
    assert result["launched_at"] == "2026-10-05T10:01:00Z"
    assert [c["type"] for c in result["conditions"]] == ["Ready"]
    assert result["conditions"][0]["message"] == "registration pending"
    assert api.calls[-1][1]["labelSelector"] == "karpenter.sh/nodepool=apps"


def test_namespace_denial_happens_before_any_read(harness):
    tools, api, _ = harness
    with pytest.raises(PermissionError):
        tools.execute("get_pods", {"namespace": "kube-system"})
    assert api.calls == []
    with pytest.raises(ValueError, match="unknown"):
        tools.execute("delete_pod", {"namespace": "payments"})
    assert api.calls == []


def test_capped_page_marks_truncation_and_never_follows_continue(harness):
    _, api, _ = harness
    api.routes["/api/v1/namespaces"]["metadata"]["continue"] = "opaque-next-page"
    tools = K8sTools(KubeReader(api, max_items=1), NamespacePolicy())
    assert tools.execute("list_namespaces", {}) == [
        {"name": "payments", "phase": "Active", "age": None}
    ]
    assert TRUNCATED.get()
    assert api.calls == [("/api/v1/namespaces", {"limit": 1})]


def test_describe_events_cap_preserves_newest_twenty(harness):
    tools, api, args = harness
    events = api.routes["/api/v1/namespaces/payments/events"]["items"]
    event = events[0]
    events[:] = [
        {**event, "lastTimestamp": f"2026-10-05T11:{minute:02d}:00Z"} for minute in range(25)
    ]
    result = tools.execute("describe_pod", args["describe_pod"])
    assert len(result["events"]) == 20
    assert result["events"][0]["last_seen"] == "2026-10-05T11:24:00Z"
    assert result["events"][-1]["last_seen"] == "2026-10-05T11:05:00Z"
    assert TRUNCATED.get()


def test_events_do_not_expose_denied_involved_namespace(harness):
    tools, api, _ = harness
    api.routes["/api/v1/namespaces/payments/events"]["items"][0]["involvedObject"]["namespace"] = (
        "kube-system"
    )
    result = tools.execute(
        "get_events", {"namespace": "payments", "type": "Warning", "since_minutes": 60}
    )
    assert all(event["object"] != "Pod/api-2-a" for event in result)


def test_foreign_namespace_objects_are_filtered_before_projection(harness):
    tools, api, _ = harness
    api.routes["/api/v1/namespaces/payments/pods"]["items"][0]["metadata"]["namespace"] = (
        "kube-system"
    )
    result = tools.execute("get_pods", {"namespace": "payments"})
    assert "api-2-a" not in [pod["name"] for pod in result]


@pytest.mark.parametrize(
    "status,exception",
    [(403, PermissionError), (404, FileNotFoundError), (504, TimeoutError), (500, RuntimeError)],
)
def test_sdk_errors_are_generic_and_do_not_leak_response_bodies(harness, status, exception):
    tools, api, args = harness
    api.routes["/api/v1/namespaces/payments/pods/api-2-a"] = {
        "__status": status,
        "message": "never-return-this",
    }
    with pytest.raises(exception) as error:
        tools.execute("describe_pod", args["describe_pod"])
    assert "never-return-this" not in str(error.value)
