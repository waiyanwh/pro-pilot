"""Read-only EKS, ALB, and CloudWatch projections for the AWS tool group.

Covers spec/tools.md #18-21: `get_eks_cluster`, `list_nodegroups`,
`get_alb_for_ingress`, `get_alb_metrics`. Every AWS call here is a `Describe*`,
`List*`, or `GetMetricData` read; no mutating action is ever issued (AGENTS.md
invariant 1). The EKS cluster name is fixed at construction time from spoke
config, never taken from tool args, so no arbitrary cluster id is accepted.
"""

from __future__ import annotations

import math
from datetime import timedelta
from typing import Any

from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from pro_pilot.protocol.messages import ErrorCode
from pro_pilot.spoke.execution import mark_truncated, remaining_seconds
from pro_pilot.spoke.pipeline import ToolFailure
from pro_pilot.spoke.policy import NamespacePolicy
from pro_pilot.spoke.tools.common import KubeReader, parse_time, utc_now

_CALL_TIMEOUT_S = 15.0
_MAX_TARGET_GROUPS = 20
_MAX_TARGETS_PER_GROUP = 50
_MAX_METRIC_POINTS = 60

_PERMISSION_CODES = {
    "AccessDenied",
    "AccessDeniedException",
    "UnauthorizedOperation",
    "AuthorizationError",
    "Forbidden",
}
_NOT_FOUND_CODES = {
    "ResourceNotFoundException",
    "ClusterNotFoundException",
    "NodegroupNotFoundException",
    "AddonNotFoundException",
    "LoadBalancerNotFoundException",
    "LoadBalancerNotFound",
    "TargetGroupNotFoundException",
    "TargetGroupNotFound",
}
_TIMEOUT_CODES = {"RequestTimeout", "RequestTimeoutException"}

# AWS/ApplicationELB-only presets (spec/tools.md #21). Each preset fixes the
# exact metric name, statistic, and unit so a caller cannot smuggle an
# arbitrary CloudWatch namespace/metric/statistic through this tool.
_METRIC_PRESETS: dict[str, tuple[str, str, str]] = {
    "requests": ("RequestCount", "Sum", "Count"),
    "http_5xx_elb": ("HTTPCode_ELB_5XX_Count", "Sum", "Count"),
    "http_5xx_target": ("HTTPCode_Target_5XX_Count", "Sum", "Count"),
    "http_4xx_target": ("HTTPCode_Target_4XX_Count", "Sum", "Count"),
    "target_response_time_p95": ("TargetResponseTime", "p95", "Seconds"),
    "unhealthy_hosts": ("UnHealthyHostCount", "Maximum", "Count"),
}
# RequestCount/HTTPCode_ELB_* are reported per LoadBalancer only. Every other
# ApplicationELB metric here is reported per (LoadBalancer, TargetGroup) pair,
# so it has to be queried once per target group and summed with metric math
# (see `_get_alb_metrics`) to get one ingress-level series.
_LB_ONLY_METRICS = {"requests", "http_5xx_elb"}
_TARGET_GROUP_METRICS = {
    "http_5xx_target",
    "http_4xx_target",
    "target_response_time_p95",
    "unhealthy_hosts",
}


class AwsTools:
    def __init__(
        self,
        eks: Any,
        elbv2: Any,
        cloudwatch: Any,
        reader: KubeReader,
        policy: NamespacePolicy,
        *,
        cluster_name: str,
        max_items: int = 200,
    ) -> None:
        self.eks = eks
        self.elbv2 = elbv2
        self.cloudwatch = cloudwatch
        self.reader = reader
        self.policy = policy
        self.cluster_name = cluster_name
        self.max_items = max_items

    def execute(self, tool_name: str, args: dict[str, Any]) -> Any:
        if tool_name == "get_eks_cluster":
            return self._get_eks_cluster()
        if tool_name == "list_nodegroups":
            return self._list_nodegroups()
        if tool_name == "get_alb_for_ingress":
            return self._get_alb_for_ingress(args["namespace"], args["ingress"])
        if tool_name == "get_alb_metrics":
            return self._get_alb_metrics(
                args["namespace"],
                args["ingress"],
                args["metrics"],
                args.get("window_minutes", 60),
            )
        raise ValueError("unsupported AWS tool")

    # -- call plumbing ----------------------------------------------------

    def _call(self, fn: Any, **kwargs: Any) -> Any:
        """Issue one bounded, non-retried AWS read and map its failures to the
        same generic exception vocabulary KubeReader uses, so the pipeline's
        existing PermissionError/FileNotFoundError/TimeoutError handling
        applies without any raw SDK error body (code or message) ever
        reaching the caller."""

        remaining_seconds(_CALL_TIMEOUT_S)
        try:
            return fn(**kwargs)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in _PERMISSION_CODES:
                raise PermissionError("AWS read access denied") from exc
            if code in _NOT_FOUND_CODES:
                raise FileNotFoundError("AWS resource not found") from exc
            if code in _TIMEOUT_CODES:
                raise TimeoutError("AWS read timed out") from exc
            raise RuntimeError("AWS read failed") from exc
        except (ConnectTimeoutError, ReadTimeoutError, EndpointConnectionError) as exc:
            raise TimeoutError("AWS read timed out") from exc

    def _check_namespace(self, namespace: str) -> None:
        if not self.policy.allowed(namespace):
            raise ToolFailure(ErrorCode.NOT_ALLOWED, "namespace is not allowed")

    # -- 18. get_eks_cluster ------------------------------------------------

    def _get_eks_cluster(self) -> dict[str, Any]:
        cluster = self._call(self.eks.describe_cluster, name=self.cluster_name)["cluster"]
        addon_names = self._list_addon_names()
        addons = [self._describe_addon(name) for name in addon_names]
        vpc_config = cluster.get("resourcesVpcConfig", {})
        return {
            "name": cluster.get("name"),
            "version": cluster.get("version"),
            "platform_version": cluster.get("platformVersion"),
            "status": cluster.get("status"),
            "endpoint_public_access": vpc_config.get("endpointPublicAccess"),
            "endpoint_private_access": vpc_config.get("endpointPrivateAccess"),
            "addons": addons,
        }

    def _list_addon_names(self) -> list[str]:
        page_size = min(self.max_items, 100)
        response = self._call(
            self.eks.list_addons, clusterName=self.cluster_name, maxResults=page_size
        )
        names = response.get("addons", [])
        if response.get("nextToken") or len(names) > page_size:
            mark_truncated()
        return names[:page_size]

    def _describe_addon(self, name: str) -> dict[str, Any]:
        addon = self._call(self.eks.describe_addon, clusterName=self.cluster_name, addonName=name)[
            "addon"
        ]
        issues = (addon.get("health") or {}).get("issues", [])
        return {
            "name": addon.get("addonName"),
            "version": addon.get("addonVersion"),
            "status": addon.get("status"),
            "health_issues": [
                {"code": issue.get("code"), "message": issue.get("message")} for issue in issues
            ],
        }

    # -- 19. list_nodegroups --------------------------------------------------

    def _list_nodegroups(self) -> list[dict[str, Any]]:
        page_size = min(self.max_items, 100)
        response = self._call(
            self.eks.list_nodegroups, clusterName=self.cluster_name, maxResults=page_size
        )
        names = response.get("nodegroups", [])
        if response.get("nextToken") or len(names) > page_size:
            mark_truncated()
        return [self._describe_nodegroup(name) for name in names[:page_size]]

    def _describe_nodegroup(self, name: str) -> dict[str, Any]:
        nodegroup = self._call(
            self.eks.describe_nodegroup, clusterName=self.cluster_name, nodegroupName=name
        )["nodegroup"]
        scaling = nodegroup.get("scalingConfig", {})
        issues = (nodegroup.get("health") or {}).get("issues", [])
        return {
            "name": nodegroup.get("nodegroupName"),
            "status": nodegroup.get("status"),
            "version": nodegroup.get("version"),
            "release_version": nodegroup.get("releaseVersion"),
            "instance_types": nodegroup.get("instanceTypes", []),
            "capacity_type": nodegroup.get("capacityType"),
            "scaling": {
                "min": scaling.get("minSize"),
                "max": scaling.get("maxSize"),
                "desired": scaling.get("desiredSize"),
            },
            "health_issues": [
                {"code": issue.get("code"), "message": issue.get("message")} for issue in issues
            ],
        }

    # -- 20. get_alb_for_ingress ----------------------------------------------

    def _get_alb_for_ingress(self, namespace: str, ingress: str) -> dict[str, Any]:
        self._check_namespace(namespace)
        load_balancer = self._resolve_load_balancer(namespace, ingress)
        target_groups = self._describe_target_groups(load_balancer["LoadBalancerArn"])
        return {
            "lb_name": load_balancer.get("LoadBalancerName"),
            "scheme": load_balancer.get("Scheme"),
            "state": (load_balancer.get("State") or {}).get("Code"),
            "target_groups": target_groups,
        }

    def _resolve_load_balancer(self, namespace: str, ingress: str) -> dict[str, Any]:
        """Resolve `status.loadBalancer.ingress[0].hostname` on the (allowed)
        ingress, then find the ALB whose DNS name matches it."""

        resource = self.reader.get(
            "networking.k8s.io/v1", "ingresses", namespace=namespace, name=ingress
        )
        lb_ingress = (resource.get("status") or {}).get("loadBalancer", {}).get("ingress", [])
        hostname = lb_ingress[0].get("hostname") if lb_ingress else None
        if not isinstance(hostname, str) or not hostname:
            raise FileNotFoundError("ingress has no load balancer hostname")
        page_size = min(self.max_items, 400)
        response = self._call(self.elbv2.describe_load_balancers, PageSize=page_size)
        load_balancers = response.get("LoadBalancers", [])
        if response.get("NextMarker") or len(load_balancers) > page_size:
            mark_truncated()
        for load_balancer in load_balancers[:page_size]:
            if (
                load_balancer.get("Type") == "application"
                and load_balancer.get("DNSName", "").lower() == hostname.lower()
            ):
                return load_balancer
        raise FileNotFoundError("load balancer not found for ingress hostname")

    def _describe_target_groups_raw(self, load_balancer_arn: str) -> list[dict[str, Any]]:
        page_size = min(_MAX_TARGET_GROUPS, self.max_items)
        response = self._call(
            self.elbv2.describe_target_groups,
            LoadBalancerArn=load_balancer_arn,
            PageSize=page_size,
        )
        groups = response.get("TargetGroups", [])
        if response.get("NextMarker") or len(groups) > page_size:
            mark_truncated()
        return groups[:page_size]

    def _describe_target_groups(self, load_balancer_arn: str) -> list[dict[str, Any]]:
        return [
            self._describe_target_group(group)
            for group in self._describe_target_groups_raw(load_balancer_arn)
        ]

    def _describe_target_group(self, group: dict[str, Any]) -> dict[str, Any]:
        health = self._call(
            self.elbv2.describe_target_health, TargetGroupArn=group["TargetGroupArn"]
        )
        descriptions = health.get("TargetHealthDescriptions", [])
        if len(descriptions) > _MAX_TARGETS_PER_GROUP:
            mark_truncated()
        targets = []
        for description in descriptions[:_MAX_TARGETS_PER_GROUP]:
            target = description.get("Target", {})
            target_health = description.get("TargetHealth", {})
            targets.append(
                {
                    "id": target.get("Id"),
                    "port": target.get("Port"),
                    "state": target_health.get("State"),
                    "reason": target_health.get("Reason"),
                    "description": target_health.get("Description"),
                }
            )
        return {
            "name": group.get("TargetGroupName"),
            "port": group.get("Port"),
            "protocol": group.get("Protocol"),
            "health_check_path": group.get("HealthCheckPath"),
            "targets": targets,
        }

    # -- 21. get_alb_metrics -------------------------------------------------

    def _get_alb_metrics(
        self, namespace: str, ingress: str, metrics: list[str], window_minutes: int = 60
    ) -> list[dict[str, Any]]:
        if not isinstance(metrics, list) or not (1 <= len(metrics) <= 6):
            raise ValueError("metrics must have between 1 and 6 entries")
        if any(name not in _METRIC_PRESETS for name in metrics):
            raise ValueError("unsupported metric preset")
        if (
            not isinstance(window_minutes, int)
            or isinstance(window_minutes, bool)
            or not (1 <= window_minutes <= 1440)
        ):
            raise ValueError("window_minutes must be between 1 and 1440")

        self._check_namespace(namespace)
        load_balancer = self._resolve_load_balancer(namespace, ingress)
        lb_dimension = _lb_dimension_value(load_balancer["LoadBalancerArn"])
        needs_target_groups = any(name in _TARGET_GROUP_METRICS for name in metrics)
        target_group_arns = (
            [
                group["TargetGroupArn"]
                for group in self._describe_target_groups_raw(load_balancer["LoadBalancerArn"])
            ]
            if needs_target_groups
            else []
        )
        period_seconds = _metric_period_seconds(window_minutes)
        end = utc_now().replace(second=0, microsecond=0)
        start = end - timedelta(minutes=window_minutes)

        queries: list[dict[str, Any]] = []
        result_ids: dict[int, str] = {}
        empty_indexes: set[int] = set()
        for index, name in enumerate(metrics):
            metric_name, stat, _ = _METRIC_PRESETS[name]
            result_id = f"m{index}"
            if name in _LB_ONLY_METRICS:
                queries.append(
                    _metric_stat_query(
                        result_id,
                        metric_name,
                        stat,
                        period_seconds,
                        [{"Name": "LoadBalancer", "Value": lb_dimension}],
                        return_data=True,
                    )
                )
                result_ids[index] = result_id
                continue
            if not target_group_arns:
                empty_indexes.add(index)
                continue
            sub_ids = []
            for group_index, group_arn in enumerate(target_group_arns):
                sub_id = f"u{index}_{group_index}"
                sub_ids.append(sub_id)
                queries.append(
                    _metric_stat_query(
                        sub_id,
                        metric_name,
                        stat,
                        period_seconds,
                        [
                            {"Name": "LoadBalancer", "Value": lb_dimension},
                            {"Name": "TargetGroup", "Value": _tg_dimension_value(group_arn)},
                        ],
                        return_data=False,
                    )
                )
            queries.append(
                {"Id": result_id, "Expression": f"SUM([{','.join(sub_ids)}])", "ReturnData": True}
            )
            result_ids[index] = result_id

        results_by_id: dict[str, dict[str, Any]] = {}
        if queries:
            response = self._call(
                self.cloudwatch.get_metric_data,
                MetricDataQueries=queries,
                StartTime=start,
                EndTime=end,
                ScanBy="TimestampAscending",
                MaxDatapoints=len(metrics) * _MAX_METRIC_POINTS,
            )
            if response.get("NextToken"):
                mark_truncated()
            results_by_id = {row["Id"]: row for row in response.get("MetricDataResults", [])}

        details = []
        for index, name in enumerate(metrics):
            if index in empty_indexes:
                details.append(_empty_metric_detail(name))
                continue
            result = results_by_id.get(result_ids[index])
            if result is None:
                mark_truncated()
                details.append(_empty_metric_detail(name))
                continue
            details.append(self._metric_details(name, result))
        return details

    def _metric_details(self, name: str, result: dict[str, Any]) -> dict[str, Any]:
        unit = _METRIC_PRESETS[name][2]
        status_code = result.get("StatusCode")
        if status_code == "PartialData":
            mark_truncated()
        elif status_code not in (None, "Complete", "PartialData"):
            raise RuntimeError("AWS metric query did not complete")

        raw_timestamps = result.get("Timestamps", [])
        values = result.get("Values", [])
        if len(raw_timestamps) != len(values):
            mark_truncated()
        parsed_timestamps = [parse_time(value) for value in raw_timestamps]
        if any(parsed is None for parsed in parsed_timestamps):
            raise RuntimeError("AWS metric timestamp is invalid")

        valid_times = [parsed for parsed in parsed_timestamps if parsed is not None]
        pairs = sorted(zip(valid_times, values, strict=False), key=lambda pair: pair[0])
        if len(pairs) > _MAX_METRIC_POINTS:
            mark_truncated()
            pairs = pairs[-_MAX_METRIC_POINTS:]
        points = [
            {"t": when.isoformat().replace("+00:00", "Z"), "v": float(value)}
            for when, value in pairs
        ]
        values_only = [point["v"] for point in points]
        summary = (
            {
                "min": min(values_only),
                "max": max(values_only),
                "avg": sum(values_only) / len(values_only),
                "last": values_only[-1],
            }
            if values_only
            else {"min": None, "max": None, "avg": None, "last": None}
        )
        return {"metric": name, "unit": unit, "points": points, "summary": summary}


def _lb_dimension_value(arn: str) -> str:
    """`LoadBalancer` dimension values drop the `loadbalancer/` ARN segment,
    e.g. `arn:...:loadbalancer/app/my-alb/50dc...` -> `app/my-alb/50dc...`."""

    return arn.rsplit("loadbalancer/", 1)[-1]


def _tg_dimension_value(arn: str) -> str:
    """`TargetGroup` dimension values keep the `targetgroup/` prefix, e.g.
    `arn:...:targetgroup/my-targets/73e2...` -> `targetgroup/my-targets/73e2...`."""

    return "targetgroup/" + arn.rsplit("targetgroup/", 1)[-1]


def _metric_stat_query(
    query_id: str,
    metric_name: str,
    stat: str,
    period_seconds: int,
    dimensions: list[dict[str, str]],
    *,
    return_data: bool,
) -> dict[str, Any]:
    return {
        "Id": query_id,
        "MetricStat": {
            "Metric": {
                "Namespace": "AWS/ApplicationELB",
                "MetricName": metric_name,
                "Dimensions": dimensions,
            },
            "Period": period_seconds,
            "Stat": stat,
        },
        "ReturnData": return_data,
    }


def _empty_metric_detail(name: str) -> dict[str, Any]:
    return {
        "metric": name,
        "unit": _METRIC_PRESETS[name][2],
        "points": [],
        "summary": {"min": None, "max": None, "avg": None, "last": None},
    }


def _metric_period_seconds(window_minutes: int) -> int:
    """Pick the smallest whole-minute CloudWatch period that keeps the point
    count at or under 60 for the requested (<=24h) window."""

    minutes_per_point = max(1, math.ceil(window_minutes / 60))
    return minutes_per_point * 60
