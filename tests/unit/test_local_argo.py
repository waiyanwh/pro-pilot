import json
from typing import Any

import pytest

from pro_pilot.protocol.messages import ErrorCode
from pro_pilot.spoke.execution import TRUNCATED
from pro_pilot.spoke.pipeline import ToolFailure
from pro_pilot.spoke.policy import NamespacePolicy
from pro_pilot.spoke.redact import redact
from pro_pilot.spoke.tools.argo import ArgoTools
from pro_pilot.spoke.tools.common import KubeReader
from pro_pilot.spoke.tools.rollouts import RolloutTools
from tests.local_support import FixtureApi, load_fixture

APPLICATION = "/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/payment"
APPLICATIONS = "/apis/argoproj.io/v1alpha1/namespaces/argocd/applications"
ROLLOUT = "/apis/argoproj.io/v1alpha1/namespaces/payment/rollouts/payment-api"
ANALYSES = "/apis/argoproj.io/v1alpha1/namespaces/payment/analysisruns"


@pytest.fixture
def fixture_data() -> dict[str, Any]:
    return load_fixture("argo")


def clients(fixture_data: dict[str, Any], *, max_items: int = 200) -> tuple[Any, ...]:
    client = FixtureApi(fixture_data["routes"])
    reader = KubeReader(client, max_items=max_items)
    policy = NamespacePolicy()
    return client, reader, ArgoTools(reader, policy), RolloutTools(reader, policy)


def test_all_four_tools_project_real_raw_responses(fixture_data: dict[str, Any]) -> None:
    client, _, argo, rollouts = clients(fixture_data)
    applications = argo.execute("list_applications", fixture_data["args"]["list_applications"])
    assert [row["name"] for row in applications] == ["payment"]
    assert applications[0]["project"] == "payments"
    assert applications[0]["revision_short"] == "abcdef1"
    assert applications[0]["last_sync"] == {
        "phase": "Failed",
        "finished_at": "2026-10-05T10:05:00Z",
    }
    application = argo.execute("get_application", fixture_data["args"]["get_application"])
    assert application["conditions"] == [{"type": "SyncError", "message": "deployment unhealthy"}]
    assert application["operation_state"]["started_at"] == "2026-10-05T10:00:00Z"
    assert application["unhealthy_resources"] == [
        {
            "kind": "Deployment",
            "namespace": "payment",
            "name": "payment-api",
            "sync": "OutOfSync",
            "health": "Degraded",
            "message": "unavailable replicas",
        }
    ]
    assert [row["revision_short"] for row in application["history"]] == ["abcdef1", "1111111"]
    rollout = rollouts.execute("get_rollout", fixture_data["args"]["get_rollout"])
    assert rollout["strategy"] == "canary"
    assert rollout["current_step"] == "2/3"
    assert rollout["set_weight"] == 20
    assert rollout["paused"] == {"reason": "CanaryPauseStep"}
    assert rollout["aborted"] is False
    assert rollout["stable_rs"] == {"hash": "stable-hash", "images": ["payment:v1"]}
    assert rollout["canary_rs"] == {"hash": "canary-hash", "images": ["payment:v2"]}
    assert rollout["replicas"] == {"desired": 4, "ready": 3, "updated": 1, "available": 3}
    assert rollout["current_analysis_runs"] == ["payment-analysis-new", "payment-background"]
    analyses = rollouts.execute("list_analysis_runs", fixture_data["args"]["list_analysis_runs"])
    assert [row["name"] for row in analyses] == ["payment-analysis-new", "payment-analysis-old"]
    assert analyses[0]["metrics"] == [
        {
            "name": "success-rate",
            "phase": "Running",
            "successful": 2,
            "failed": 1,
            "inconclusive": 0,
            "error": 0,
            "last_measurement": {
                "value": "0.95",
                "message": "threshold met",
                "finished_at": "2026-10-05T10:01:00Z",
            },
        }
    ]
    output = json.dumps([applications, application, rollout, analyses])
    for forbidden in (
        "forbidden-value",
        "forbidden-query",
        "private.invalid",
        "managedFields",
        "denied-resource",
        "wrong-owner",
        "provider",
        "headers",
        "env",
    ):
        assert forbidden not in output
    assert all(
        query["limit"] == 200
        for path, query in client.calls
        if path.endswith(("applications", "replicasets", "analysisruns"))
    )


@pytest.mark.parametrize(
    "args, expected",
    [
        ({"name_contains": "pay", "sync": "OutOfSync", "health": "Degraded"}, ["payment"]),
        ({"name_contains": "other"}, []),
        ({"sync": "Synced"}, []),
        ({"health": "Healthy"}, []),
    ],
)
def test_application_filters(
    fixture_data: dict[str, Any], args: dict[str, Any], expected: list[str]
) -> None:
    _, _, argo, _ = clients(fixture_data)
    assert [row["name"] for row in argo.execute("list_applications", args)] == expected


@pytest.mark.parametrize("destination", [{"namespace": "kube-system"}, {}, {"namespace": None}])
def test_denied_or_missing_application_destination_fails_closed(
    fixture_data: dict[str, Any], destination: dict[str, Any]
) -> None:
    fixture_data["routes"][APPLICATION]["spec"]["destination"] = destination
    _, _, argo, _ = clients(fixture_data)
    with pytest.raises(ToolFailure) as error:
        argo.execute("get_application", {"name": "payment"})
    assert error.value.code == ErrorCode.NOT_ALLOWED


@pytest.mark.parametrize(
    "tool, args",
    [
        ("get_rollout", {"namespace": "kube-system", "name": "payment-api"}),
        ("list_analysis_runs", {"namespace": "kube-system"}),
    ],
)
def test_denied_rollout_namespace_makes_no_reads(
    fixture_data: dict[str, Any], tool: str, args: dict[str, Any]
) -> None:
    client, _, _, rollouts = clients(fixture_data)
    with pytest.raises(ToolFailure) as error:
        rollouts.execute(tool, args)
    assert error.value.code == ErrorCode.NOT_ALLOWED
    assert client.calls == []


def test_application_resource_policy_and_caps(fixture_data: dict[str, Any]) -> None:
    status = fixture_data["routes"][APPLICATION]["status"]
    resource = status["resources"][0]
    status["resources"] = [dict(resource, name=f"api-{i}") for i in range(51)] + [
        dict(resource, namespace="argocd", name="denied"),
        dict(resource, namespace=None, name="cluster-scoped"),
        dict(resource, namespace="", name="unscoped"),
    ]
    status["history"] = [
        {"revision": f"revision-{i}", "deployedAt": f"2026-10-0{i}T00:00:00Z"} for i in range(1, 7)
    ]
    _, _, argo, _ = clients(fixture_data)
    token = TRUNCATED.set(False)
    try:
        result = argo.execute("get_application", {"name": "payment"})
        assert TRUNCATED.get() is True
    finally:
        TRUNCATED.reset(token)
    assert len(result["unhealthy_resources"]) == 50
    assert all(row["namespace"] == "payment" for row in result["unhealthy_resources"])
    assert len(result["history"]) == 5
    assert result["history"][0]["deployed_at"] == "2026-10-06T00:00:00Z"


def test_analysis_owner_filter_precedes_limit_and_sets_truncation(
    fixture_data: dict[str, Any],
) -> None:
    _, _, _, rollouts = clients(fixture_data)
    token = TRUNCATED.set(False)
    try:
        runs = rollouts.execute(
            "list_analysis_runs", {"namespace": "payment", "rollout": "payment-api", "limit": 1}
        )
        assert [row["name"] for row in runs] == ["payment-analysis-new"]
        assert TRUNCATED.get() is True
    finally:
        TRUNCATED.reset(token)
    assert (
        rollouts.execute(
            "list_analysis_runs", {"namespace": "payment", "rollout": "nonexistent", "limit": 1}
        )
        == []
    )


def test_analysis_without_rollout_includes_all_and_empty_measurements(
    fixture_data: dict[str, Any],
) -> None:
    fixture_data["routes"][ANALYSES]["items"][1]["status"]["metricResults"] = [{"name": "empty"}]
    _, _, _, rollouts = clients(fixture_data)
    runs = rollouts.execute("list_analysis_runs", {"namespace": "payment", "limit": 10})
    assert runs[0]["name"] == "unrelated-new"
    assert runs[0]["metrics"][0]["last_measurement"] is None
    assert runs[0]["metrics"][0]["successful"] == 0


def test_blue_green_selectors_pause_abort_and_analyses(fixture_data: dict[str, Any]) -> None:
    rollout = fixture_data["routes"][ROLLOUT]
    rollout["spec"]["strategy"] = {
        "blueGreen": {"activeService": "payment", "previewService": "preview"}
    }
    rollout["spec"]["paused"] = True
    rollout["status"] = {
        "phase": "Degraded",
        "abort": True,
        "blueGreen": {
            "activeSelector": "stable-hash",
            "previewSelector": "canary-hash",
            "prePromotionAnalysisRunStatus": {"name": "pre-check"},
            "postPromotionAnalysisRunStatus": {"name": "post-check"},
        },
    }
    _, _, _, rollouts = clients(fixture_data)
    result = rollouts.execute("get_rollout", {"namespace": "payment", "name": "payment-api"})
    assert result["strategy"] == "blueGreen"
    assert result["current_step"] is None and result["set_weight"] is None
    assert result["paused"] == {"reason": "UserPause"}
    assert result["aborted"] is True
    assert result["stable_rs"]["images"] == ["payment:v1"]
    assert result["canary_rs"]["images"] == ["payment:v2"]
    assert result["current_analysis_runs"] == ["pre-check", "post-check"]


def test_canary_step_weight_fallback_and_completed_steps(fixture_data: dict[str, Any]) -> None:
    status = fixture_data["routes"][ROLLOUT]["status"]
    status.pop("canary")
    status["currentStepIndex"] = 3
    status["currentPodHash"] = status["stableRS"]
    _, _, _, rollouts = clients(fixture_data)
    result = rollouts.execute("get_rollout", {"namespace": "payment", "name": "payment-api"})
    assert result["current_step"] == "3/3"
    assert result["set_weight"] == 50
    assert result["canary_rs"] is None


def test_discovered_version_and_custom_argo_namespace(fixture_data: dict[str, Any]) -> None:
    fixture_data["routes"] = {
        path.replace("argoproj.io/v1alpha1", "argoproj.io/v1beta1").replace(
            "/namespaces/argocd/", "/namespaces/gitops/"
        ): payload
        for path, payload in fixture_data["routes"].items()
    }
    client, reader, _, rollouts = clients(fixture_data)
    reader.api_versions["argoproj.io"] = "argoproj.io/v1beta1"
    argo = ArgoTools(reader, NamespacePolicy(), argocd_namespace="gitops")
    assert argo.execute("list_applications", {})[0]["name"] == "payment"
    assert argo.execute("get_application", {"name": "payment"})["name"] == "payment"
    assert (
        rollouts.execute("get_rollout", {"namespace": "payment", "name": "payment-api"})["phase"]
        == "Paused"
    )
    assert (
        rollouts.execute("list_analysis_runs", {"namespace": "payment"})[0]["name"]
        == "unrelated-new"
    )
    assert all("argoproj.io/v1beta1" in path for path, _ in client.calls if "argoproj.io" in path)


def test_one_page_list_cap_reports_truncation(fixture_data: dict[str, Any]) -> None:
    fixture_data["routes"][APPLICATIONS]["metadata"]["continue"] = "next-page"
    client, _, argo, _ = clients(fixture_data, max_items=1)
    token = TRUNCATED.set(False)
    try:
        assert len(argo.execute("list_applications", {})) == 1
        assert TRUNCATED.get() is True
    finally:
        TRUNCATED.reset(token)
    assert len(client.calls) == 1
    assert client.calls[0][1] == {"limit": 1}


def test_conditions_and_metrics_follow_common_list_cap(fixture_data: dict[str, Any]) -> None:
    fixture_data["routes"][APPLICATION]["status"]["conditions"] = [
        {"type": "SyncError", "message": "first"},
        {"type": "SyncError", "message": "second"},
    ]
    fixture_data["routes"][ANALYSES]["items"] = [fixture_data["routes"][ANALYSES]["items"][2]]
    fixture_data["routes"][ANALYSES]["items"][0]["status"]["metricResults"] = [
        {"name": "first"},
        {"name": "second"},
    ]
    _, _, argo, rollouts = clients(fixture_data, max_items=1)
    token = TRUNCATED.set(False)
    try:
        result = argo.execute("get_application", {"name": "payment"})
        assert result["conditions"] == [{"type": "SyncError", "message": "first"}]
        assert TRUNCATED.get() is True
        TRUNCATED.set(False)
        runs = rollouts.execute("list_analysis_runs", {"namespace": "payment", "limit": 5})
        assert [row["name"] for row in runs[0]["metrics"]] == ["first"]
        assert TRUNCATED.get() is True
    finally:
        TRUNCATED.reset(token)


def test_analysis_order_uses_instants_and_creation_fallback(fixture_data: dict[str, Any]) -> None:
    fixture_data["routes"][ANALYSES]["items"] = [
        {"metadata": {"name": "whole-second"}, "status": {"startedAt": "2026-10-05T10:00:00Z"}},
        {
            "metadata": {"name": "fractional-second"},
            "status": {"startedAt": "2026-10-05T10:00:00.5Z"},
        },
        {
            "metadata": {"name": "creation-only", "creationTimestamp": "2026-10-05T12:00:01+02:00"},
            "status": {},
        },
    ]
    _, _, _, rollouts = clients(fixture_data)
    runs = rollouts.execute("list_analysis_runs", {"namespace": "payment", "limit": 5})
    assert [row["name"] for row in runs] == ["creation-only", "fractional-second", "whole-second"]


def test_projected_argo_free_text_is_redactable(fixture_data: dict[str, Any]) -> None:
    message = "password=hunter2"
    application = fixture_data["routes"][APPLICATION]["status"]
    application["conditions"][0]["message"] = message
    application["operationState"]["message"] = message
    application["resources"][0]["health"]["message"] = message
    fixture_data["routes"][ROLLOUT]["status"]["message"] = message
    analysis = fixture_data["routes"][ANALYSES]["items"][2]["status"]
    analysis["message"] = message
    analysis["metricResults"][0]["measurements"][-1]["message"] = message
    _, _, argo, rollouts = clients(fixture_data)
    outputs = [
        argo.execute("get_application", {"name": "payment"}),
        rollouts.execute("get_rollout", {"namespace": "payment", "name": "payment-api"}),
        rollouts.execute("list_analysis_runs", {"namespace": "payment"}),
    ]
    redacted, count = redact(outputs)
    assert count == 6
    assert "hunter2" not in json.dumps(redacted)
    assert "[REDACTED:kv_secret]" in json.dumps(redacted)
