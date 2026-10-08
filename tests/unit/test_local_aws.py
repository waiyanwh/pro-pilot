"""Offline tests of AWS projection, query bounds, and namespace enforcement."""

from __future__ import annotations

import json
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError
from botocore.session import Session
from botocore.validate import validate_parameters

from pro_pilot.protocol.messages import ErrorCode
from pro_pilot.spoke.execution import DEADLINE, TRUNCATED
from pro_pilot.spoke.pipeline import ToolFailure
from pro_pilot.spoke.policy import NamespacePolicy
from pro_pilot.spoke.tools.aws import AwsTools
from pro_pilot.spoke.tools.common import KubeReader

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "local" / "aws.json"
_NOW = datetime(2026, 10, 5, 12, tzinfo=UTC)


class AwsResponses:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _response(self, name: str, kwargs: dict[str, Any]) -> Any:
        self.calls.append((name, kwargs))
        response = self.responses[name]
        if isinstance(response, Exception):
            raise response
        return deepcopy(response)

    def describe_cluster(self, **kwargs: Any) -> Any:
        return self._response("describe_cluster", kwargs)

    def list_addons(self, **kwargs: Any) -> Any:
        return self._response("list_addons", kwargs)

    def describe_addon(self, **kwargs: Any) -> Any:
        return self._response("describe_addon", kwargs)

    def list_nodegroups(self, **kwargs: Any) -> Any:
        return self._response("list_nodegroups", kwargs)

    def describe_nodegroup(self, **kwargs: Any) -> Any:
        return self._response("describe_nodegroup", kwargs)

    def describe_load_balancers(self, **kwargs: Any) -> Any:
        return self._response("describe_load_balancers", kwargs)

    def describe_target_groups(self, **kwargs: Any) -> Any:
        return self._response("describe_target_groups", kwargs)

    def describe_target_health(self, **kwargs: Any) -> Any:
        return self._response("describe_target_health", kwargs)

    def get_metric_data(self, **kwargs: Any) -> Any:
        return self._response("get_metric_data", kwargs)


class IngressResponses:
    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def get(self, version: str, resource: str, *, namespace: str, name: str) -> Any:
        uri = f"/apis/{version}/namespaces/{namespace}/{resource}/{name}"
        self.calls.append(uri)
        return deepcopy(self.routes[uri])


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch) -> Any:
    fixture = json.loads(_FIXTURE.read_text())
    sdk = AwsResponses(fixture["aws"])
    reader = IngressResponses(fixture["routes"])
    tools = AwsTools(
        sdk,
        sdk,
        sdk,
        cast(KubeReader, reader),
        NamespacePolicy(),
        cluster_name="fixture-cluster",
    )
    monkeypatch.setattr("pro_pilot.spoke.tools.aws.utc_now", lambda: _NOW)
    truncated = TRUNCATED.set(False)
    deadline = DEADLINE.set(None)
    yield tools, sdk, reader, fixture
    TRUNCATED.reset(truncated)
    DEADLINE.reset(deadline)


def test_eks_safe_fields_and_fixed_cluster(setup: Any) -> None:
    tools, sdk, _, _ = setup
    cluster = tools.execute("get_eks_cluster", {"cluster_name": "other"})
    assert cluster["name"] == "fixture-cluster"
    assert cluster["endpoint_public_access"] is False
    assert cluster["endpoint_private_access"] is True
    assert cluster["addons"][0]["health_issues"] == [
        {"code": "InsufficientNumberOfReplicas", "message": "one replica unavailable"},
    ]
    assert sdk.calls == [
        ("describe_cluster", {"name": "fixture-cluster"}),
        ("list_addons", {"clusterName": "fixture-cluster", "maxResults": 100}),
        ("describe_addon", {"clusterName": "fixture-cluster", "addonName": "vpc-cni"}),
    ]
    assert "must-not-leave" not in json.dumps(cluster)
    assert "endpoint" not in cluster and "certificateAuthority" not in cluster


def test_nodegroups_safe_scaling_and_issues(setup: Any) -> None:
    tools, sdk, _, _ = setup
    groups = tools.execute("list_nodegroups", {})
    assert groups[0]["scaling"] == {"min": 1, "max": 4, "desired": 2}
    assert groups[0]["health_issues"] == [
        {"code": "NodeCreationFailure", "message": "a node did not join"},
    ]
    assert groups[0]["instance_types"] == ["m7i.large"]
    assert "must-not-leave" not in json.dumps(groups)
    assert sdk.calls[-1][1] == {"clusterName": "fixture-cluster", "nodegroupName": "workers"}


def test_alb_resolves_dns_not_first_lb_and_projects_target_health(setup: Any) -> None:
    tools, sdk, reader, fixture = setup
    result = tools.execute("get_alb_for_ingress", fixture["args"]["get_alb_for_ingress"])
    assert result["lb_name"] == "payment-alb"
    assert result["scheme"] == "internal" and result["state"] == "active"
    target = result["target_groups"][0]["targets"][0]
    assert target == {
        "id": "10.0.1.9",
        "port": 8080,
        "state": "unhealthy",
        "reason": "Target.ResponseCodeMismatch",
        "description": "health check returned 503",
    }
    assert "must-not-leave" not in json.dumps(result)
    assert reader.calls == list(fixture["routes"])
    assert sdk.calls[1][1]["PageSize"] == 20
    assert sdk.calls[1][1]["LoadBalancerArn"].endswith("app/payment-alb/123")


@pytest.mark.parametrize("tool", ["get_alb_for_ingress", "get_alb_metrics"])
def test_denied_namespace_never_reads_kubernetes_or_aws(setup: Any, tool: str) -> None:
    tools, sdk, reader, fixture = setup
    args = {**fixture["args"][tool], "namespace": "kube-system"}
    with pytest.raises(ToolFailure, match="is not allowed") as failure:
        tools.execute(tool, args)
    assert failure.value.code == ErrorCode.NOT_ALLOWED
    assert not sdk.calls and not reader.calls


@pytest.mark.parametrize("missing", ["hostname", "match", "application"])
def test_missing_ingress_alb_is_not_found(setup: Any, missing: str) -> None:
    tools, sdk, reader, fixture = setup
    if missing == "hostname":
        reader.routes[next(iter(reader.routes))]["status"] = {}
    elif missing == "match":
        sdk.responses["describe_load_balancers"]["LoadBalancers"] = []
    else:
        sdk.responses["describe_load_balancers"]["LoadBalancers"][1]["Type"] = "network"
    with pytest.raises(FileNotFoundError):
        tools.execute("get_alb_for_ingress", fixture["args"]["get_alb_for_ingress"])
    assert all(name != "describe_target_groups" for name, _ in sdk.calls)


@pytest.mark.parametrize(
    "method,key,token",
    [
        ("list_addons", "addons", "nextToken"),
        ("list_nodegroups", "nodegroups", "nextToken"),
        ("describe_load_balancers", "LoadBalancers", "NextMarker"),
        ("describe_target_groups", "TargetGroups", "NextMarker"),
        ("get_metric_data", "MetricDataResults", "NextToken"),
    ],
)
def test_one_page_tokens_mark_truncation(setup: Any, method: str, key: str, token: str) -> None:
    tools, sdk, _, fixture = setup
    sdk.responses[method][token] = "private-token"
    tool = {
        "list_addons": "get_eks_cluster",
        "list_nodegroups": "list_nodegroups",
        "describe_load_balancers": "get_alb_for_ingress",
        "describe_target_groups": "get_alb_for_ingress",
        "get_metric_data": "get_alb_metrics",
    }[method]
    result = tools.execute(tool, fixture["args"][tool])
    assert TRUNCATED.get()
    assert len([name for name, _ in sdk.calls if name == method]) == 1
    assert "private-token" not in json.dumps(result)
    assert key in sdk.responses[method]


def test_group_and_target_caps(setup: Any) -> None:
    tools, sdk, _, fixture = setup
    group = sdk.responses["describe_target_groups"]["TargetGroups"][0]
    target = sdk.responses["describe_target_health"]["TargetHealthDescriptions"][0]
    sdk.responses["describe_target_groups"]["TargetGroups"] = [group] * 21
    sdk.responses["describe_target_health"]["TargetHealthDescriptions"] = [target] * 51
    result = tools.execute("get_alb_for_ingress", fixture["args"]["get_alb_for_ingress"])
    assert len(result["target_groups"]) == 20
    assert all(len(group["targets"]) == 50 for group in result["target_groups"])
    assert len([name for name, _ in sdk.calls if name == "describe_target_health"]) == 20
    assert TRUNCATED.get()


def test_configured_item_cap_applies_to_sdk_pages(setup: Any) -> None:
    tools, sdk, _, fixture = setup
    tools.max_items = 1
    sdk.responses["list_nodegroups"]["nodegroups"] = ["workers", "omitted"]
    assert len(tools.execute("list_nodegroups", {})) == 1
    assert sdk.calls[0][1]["maxResults"] == 1
    assert TRUNCATED.get()
    sdk.calls.clear()
    sdk.responses["describe_load_balancers"]["LoadBalancers"].pop(0)
    tools.execute("get_alb_for_ingress", fixture["args"]["get_alb_for_ingress"])
    assert sdk.calls[0][1]["PageSize"] == 1
    assert sdk.calls[1][1]["PageSize"] == 1


def test_metric_queries_presets_dimensions_sort_and_summary(setup: Any) -> None:
    tools, sdk, _, fixture = setup
    result = tools.execute("get_alb_metrics", fixture["args"]["get_alb_metrics"])
    assert result[0]["points"] == [
        {"t": "2026-10-05T11:01:00Z", "v": 6},
        {"t": "2026-10-05T11:59:00Z", "v": 12},
    ]
    assert result[0]["summary"] == {"min": 6, "max": 12, "avg": 9, "last": 12}
    assert result[4]["unit"] == "Seconds"
    request = sdk.calls[-1][1]
    assert request["StartTime"] == _NOW - timedelta(hours=1)
    assert request["EndTime"] == _NOW
    assert request["ScanBy"] == "TimestampAscending" and request["MaxDatapoints"] == 360
    stats = [query["MetricStat"] for query in request["MetricDataQueries"] if "MetricStat" in query]
    assert [stat["Stat"] for stat in stats] == ["Sum", "Sum", "Sum", "Sum", "p95", "Maximum"]
    assert all(stat["Period"] == 60 for stat in stats)
    assert all(stat["Metric"]["Namespace"] == "AWS/ApplicationELB" for stat in stats)
    assert stats[0]["Metric"]["Dimensions"] == [
        {"Name": "LoadBalancer", "Value": "app/payment-alb/123"},
    ]
    assert stats[-1]["Metric"]["Dimensions"][-1] == {
        "Name": "TargetGroup",
        "Value": "targetgroup/payment-targets/456",
    }
    assert request["MetricDataQueries"][-1]["Expression"] == "SUM([u5_0])"
    assert not TRUNCATED.get()


@pytest.mark.parametrize("window,period", [(1, 60), (61, 120), (1440, 1440)])
def test_metric_window_and_period_bound(setup: Any, window: int, period: int) -> None:
    tools, sdk, _, fixture = setup
    args = {**fixture["args"]["get_alb_metrics"], "window_minutes": window, "metrics": ["requests"]}
    tools.execute("get_alb_metrics", args)
    request = sdk.calls[-1][1]
    assert request["EndTime"] - request["StartTime"] == timedelta(minutes=window)
    assert request["MetricDataQueries"][0]["MetricStat"]["Period"] == period
    assert window * 60 / period <= 60


@pytest.mark.parametrize("change", ["partial", "token", "too_many", "mismatch", "missing"])
def test_metric_partial_results_mark_truncated(setup: Any, change: str) -> None:
    tools, sdk, _, fixture = setup
    response = sdk.responses["get_metric_data"]
    row = response["MetricDataResults"][0]
    if change == "partial":
        row["StatusCode"] = "PartialData"
    elif change == "token":
        response["NextToken"] = "omitted-page"
    elif change == "too_many":
        row["Timestamps"] = [(_NOW - timedelta(seconds=i + 1)).isoformat() for i in range(61)]
        row["Values"] = list(range(61))
    elif change == "mismatch":
        row["Values"] = [1]
    else:
        response["MetricDataResults"] = []
    result = tools.execute("get_alb_metrics", fixture["args"]["get_alb_metrics"])
    assert TRUNCATED.get()
    assert all(len(row["points"]) <= 60 for row in result)


def test_metrics_without_targetgroups_returns_empty_unhealthy_series(setup: Any) -> None:
    tools, sdk, _, fixture = setup
    sdk.responses["describe_target_groups"]["TargetGroups"] = []
    result = tools.execute(
        "get_alb_metrics",
        {
            **fixture["args"]["get_alb_metrics"],
            "metrics": ["unhealthy_hosts"],
        },
    )
    assert result == [
        {
            "metric": "unhealthy_hosts",
            "unit": "Count",
            "points": [],
            "summary": {"min": None, "max": None, "avg": None, "last": None},
        }
    ]
    assert all(name != "get_metric_data" for name, _ in sdk.calls)


@pytest.mark.parametrize(
    "code,status,error",
    [
        ("AccessDeniedException", 403, PermissionError),
        ("ResourceNotFoundException", 404, FileNotFoundError),
        ("LoadBalancerNotFound", 400, FileNotFoundError),
        ("TargetGroupNotFound", 400, FileNotFoundError),
        ("RequestTimeout", 400, TimeoutError),
        ("ThrottlingException", 429, RuntimeError),
    ],
)
def test_sdk_errors_are_generic(setup: Any, code: str, status: int, error: type[Exception]) -> None:
    tools, sdk, _, _ = setup
    sdk.responses["describe_cluster"] = ClientError(
        {
            "Error": {"Code": code, "Message": "secret=value internal-account"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "DescribeCluster",
    )
    with pytest.raises(error) as caught:
        tools.execute("get_eks_cluster", {})
    assert "secret" not in str(caught.value) and "internal-account" not in str(caught.value)


def test_sdk_read_timeout_is_generic(setup: Any) -> None:
    tools, sdk, _, _ = setup
    sdk.responses["describe_cluster"] = ReadTimeoutError(endpoint_url="https://private.invalid")
    with pytest.raises(TimeoutError, match="AWS read timed out"):
        tools.execute("get_eks_cluster", {})


def test_deadline_checked_before_each_sdk_call(setup: Any) -> None:
    tools, sdk, _, _ = setup
    DEADLINE.set(time.monotonic() - 1)
    with pytest.raises(TimeoutError):
        tools.execute("get_eks_cluster", {})
    assert not sdk.calls


def test_deadline_between_sdk_calls_stops_fanout(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools, sdk, _, _ = setup
    original = sdk.list_addons

    def expire(**kwargs: Any) -> Any:
        response = original(**kwargs)
        DEADLINE.set(time.monotonic() - 1)
        return response

    monkeypatch.setattr(sdk, "list_addons", expire)
    with pytest.raises(TimeoutError):
        tools.execute("get_eks_cluster", {})
    assert [name for name, _ in sdk.calls] == ["describe_cluster", "list_addons"]


def test_sdk_request_members_match_installed_service_models(setup: Any) -> None:
    tools, sdk, _, fixture = setup
    for tool, args in fixture["args"].items():
        tools.execute(tool, args)
    session = Session()
    operations = {
        "describe_cluster": ("eks", "DescribeCluster"),
        "list_addons": ("eks", "ListAddons"),
        "describe_addon": ("eks", "DescribeAddon"),
        "list_nodegroups": ("eks", "ListNodegroups"),
        "describe_nodegroup": ("eks", "DescribeNodegroup"),
        "describe_load_balancers": ("elbv2", "DescribeLoadBalancers"),
        "describe_target_groups": ("elbv2", "DescribeTargetGroups"),
        "describe_target_health": ("elbv2", "DescribeTargetHealth"),
        "get_metric_data": ("cloudwatch", "GetMetricData"),
    }

    for name, kwargs in sdk.calls:
        service, operation = operations[name]
        shape = session.get_service_model(service).operation_model(operation).input_shape
        validate_parameters(kwargs, shape)


@pytest.mark.parametrize(
    "args",
    [
        {"metrics": ["requests"], "window_minutes": 1441},
        {"metrics": ["requests"], "window_minutes": 0},
        {"metrics": ["arbitrary_metric"]},
        {"metrics": []},
    ],
)
def test_invalid_metric_queries_never_read_upstream(setup: Any, args: dict[str, Any]) -> None:
    tools, sdk, reader, fixture = setup
    with pytest.raises(ValueError):
        tools.execute("get_alb_metrics", {**fixture["args"]["get_alb_metrics"], **args})
    assert not sdk.calls and not reader.calls


def test_unhealthy_hosts_uses_every_bounded_target_group(setup: Any) -> None:
    tools, sdk, _, fixture = setup
    second = deepcopy(sdk.responses["describe_target_groups"]["TargetGroups"][0])
    second["TargetGroupArn"] = second["TargetGroupArn"].replace("/456", "/789")
    sdk.responses["describe_target_groups"]["TargetGroups"].append(second)
    tools.execute(
        "get_alb_metrics",
        {
            **fixture["args"]["get_alb_metrics"],
            "metrics": ["unhealthy_hosts"],
        },
    )
    queries = sdk.calls[-1][1]["MetricDataQueries"]
    assert queries[-1]["Expression"] == "SUM([u0_0,u0_1])"
    assert [query["ReturnData"] for query in queries] == [False, False, True]
    assert queries[1]["MetricStat"]["Metric"]["Dimensions"][-1]["Value"].endswith("/789")


def test_metric_timestamps_accept_mixed_datetime_and_string(setup: Any) -> None:
    tools, sdk, _, fixture = setup
    row = sdk.responses["get_metric_data"]["MetricDataResults"][0]
    row["Timestamps"][0] = datetime(2026, 10, 5, 11, 59, tzinfo=UTC)
    result = tools.execute("get_alb_metrics", fixture["args"]["get_alb_metrics"])
    assert result[0]["points"] == [
        {"t": "2026-10-05T11:01:00Z", "v": 6.0},
        {"t": "2026-10-05T11:59:00Z", "v": 12.0},
    ]


def test_invalid_metric_timestamp_returns_safe_generic_error(setup: Any) -> None:
    tools, sdk, _, fixture = setup
    sdk.responses["get_metric_data"]["MetricDataResults"][0]["Timestamps"] = [
        "secret=value private-account",
        "2026-10-05T11:01:00Z",
    ]
    with pytest.raises(RuntimeError, match="AWS metric timestamp is invalid") as failure:
        tools.execute("get_alb_metrics", fixture["args"]["get_alb_metrics"])
    assert "secret" not in str(failure.value)


def test_unknown_sdk_error_code_does_not_leak(setup: Any) -> None:
    tools, sdk, _, _ = setup
    sdk.responses["describe_cluster"] = ClientError(
        {
            "Error": {"Code": "secret=value", "Message": "private-message"},
        },
        "DescribeCluster",
    )
    with pytest.raises(RuntimeError) as failure:
        tools.execute("get_eks_cluster", {})
    assert str(failure.value) == "AWS read failed"
