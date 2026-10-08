"""Single source of truth for tool arguments, outputs, and Bedrock metadata."""
# pyright: reportUnknownVariableType=false

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator, model_validator


class ToolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class OutputModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EmptyArgs(ToolModel):
    pass


class WorkloadRef(ToolModel):
    kind: Literal["deployment", "statefulset", "daemonset", "job", "cronjob"]
    name: str = Field(min_length=1, max_length=253)


class NamespaceArgs(ToolModel):
    namespace: str = Field(min_length=1, max_length=253)


class NameContainsArgs(ToolModel):
    name_contains: str | None = Field(default=None, max_length=100)


class ListNamespacesArgs(NameContainsArgs):
    pass


class ListWorkloadsArgs(NamespaceArgs):
    kind: Literal["deployment", "statefulset", "daemonset", "job", "cronjob"] | None = None
    name_contains: str | None = Field(default=None, max_length=100)


class GetWorkloadArgs(NamespaceArgs):
    kind: Literal["deployment", "statefulset", "daemonset"]
    name: str = Field(min_length=1, max_length=253)


_SELECTOR_PATTERN = re.compile(r"^[A-Za-z0-9._/\-=!, ()]+$")

# Kubernetes label selector grammar (a subset sufficient to reject malformed
# selectors that the character-class regex above lets through, e.g. "a==,,",
# "a in (", or unbalanced parentheses). See
# https://kubernetes.io/docs/concepts/overview/working-with-objects/labels/#label-selectors
_LABEL_PART = r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,61}[A-Za-z0-9])?"
_LABEL_KEY = rf"(?:{_LABEL_PART}/)?{_LABEL_PART}"
_LABEL_VALUE = rf"(?:{_LABEL_PART})?"
_SET_VALUES = rf"\(\s*{_LABEL_VALUE}(?:\s*,\s*{_LABEL_VALUE})*\s*\)"
_REQUIREMENT_PATTERN = re.compile(
    rf"^\s*!\s*{_LABEL_KEY}\s*$"
    rf"|^\s*{_LABEL_KEY}\s*$"
    rf"|^\s*{_LABEL_KEY}\s*(?:==|=|!=)\s*{_LABEL_VALUE}\s*$"
    rf"|^\s*{_LABEL_KEY}\s+(?:in|notin)\s*{_SET_VALUES}\s*$"
)


def _split_selector_requirements(value: str) -> list[str] | None:
    """Split on top-level commas, ignoring commas nested inside ``(...)``."""

    parts: list[str] = []
    depth = 0
    current = ""
    for char in value:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return None
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    if depth != 0:
        return None
    parts.append(current)
    return parts


def _is_valid_label_selector(value: str) -> bool:
    """Parse-check a label selector against the Kubernetes requirement grammar,
    beyond the permissive character-class check."""

    if not value.strip():
        return False
    requirements = _split_selector_requirements(value)
    if requirements is None:
        return False
    return all(_REQUIREMENT_PATTERN.fullmatch(part.strip()) for part in requirements)


class GetPodsArgs(NamespaceArgs):
    workload: WorkloadRef | None = None
    label_selector: str | None = Field(default=None, max_length=256)
    status: Literal["all", "not_ready", "restarting", "pending"] = "all"

    @field_validator("label_selector")
    @classmethod
    def validate_selector(cls, value: str | None) -> str | None:
        if value is not None and (
            not _SELECTOR_PATTERN.fullmatch(value) or not _is_valid_label_selector(value)
        ):
            raise ValueError("invalid Kubernetes label selector")
        return value


class DescribePodArgs(NamespaceArgs):
    name: str = Field(min_length=1, max_length=253)


class GetEventsArgs(NamespaceArgs):
    involved_kind: str | None = Field(default=None, max_length=100)
    involved_name: str | None = Field(default=None, max_length=253)
    type: Literal["Warning", "Normal", "all"] = "Warning"
    since_minutes: int = Field(default=60, ge=1, le=180)


class GetPodLogsArgs(NamespaceArgs):
    pod: str = Field(min_length=1, max_length=253)
    container: str | None = Field(default=None, max_length=253)
    tail_lines: int = Field(default=100, ge=1, le=200)
    since_minutes: int = Field(default=15, ge=1, le=60)
    previous: bool = False
    contains: str | None = Field(default=None, max_length=100)


class ListServicesArgs(NamespaceArgs):
    pass


class ListIngressesArgs(NamespaceArgs):
    pass


class ListHpasArgs(NamespaceArgs):
    pass


class GetNodesArgs(ToolModel):
    status: Literal["all", "not_ready", "pressure"] = "all"
    label_selector: str | None = Field(default=None, max_length=256)

    @field_validator("label_selector")
    @classmethod
    def validate_selector(cls, value: str | None) -> str | None:
        if value is not None and (
            not _SELECTOR_PATTERN.fullmatch(value) or not _is_valid_label_selector(value)
        ):
            raise ValueError("invalid Kubernetes label selector")
        return value


class TopPodsArgs(NamespaceArgs):
    workload: WorkloadRef | None = None


class ListNodeclaimsArgs(ToolModel):
    nodepool: str | None = Field(default=None, max_length=253)


class ListApplicationsArgs(ToolModel):
    name_contains: str | None = Field(default=None, max_length=100)
    sync: Literal["Synced", "OutOfSync", "Unknown"] | None = None
    health: (
        Literal[
            "Healthy",
            "Progressing",
            "Degraded",
            "Suspended",
            "Missing",
            "Unknown",
        ]
        | None
    ) = None


class GetApplicationArgs(ToolModel):
    name: str = Field(min_length=1, max_length=253)


class GetRolloutArgs(NamespaceArgs):
    name: str = Field(min_length=1, max_length=253)


class ListAnalysisRunsArgs(NamespaceArgs):
    rollout: str | None = Field(default=None, max_length=253)
    limit: int = Field(default=5, ge=1, le=10)


class GetAlbForIngressArgs(NamespaceArgs):
    ingress: str = Field(min_length=1, max_length=253)


class GetAlbMetricsArgs(NamespaceArgs):
    ingress: str = Field(min_length=1, max_length=253)
    metrics: list[
        Literal[
            "requests",
            "http_5xx_elb",
            "http_5xx_target",
            "http_4xx_target",
            "target_response_time_p95",
            "unhealthy_hosts",
        ]
    ] = Field(min_length=1, max_length=6)
    window_minutes: int = Field(default=60, ge=1, le=1440)


class ClusterSummary(OutputModel):
    cluster_id: str
    connected: bool
    labels: dict[str, str] = Field(default_factory=dict)
    capabilities: list[str] = Field(default_factory=list)


class ListClustersOutput(RootModel[list[ClusterSummary]]):
    pass


class NamespaceSummary(OutputModel):
    name: str
    phase: str | None = None
    age: str | None = None


class WorkloadSummary(OutputModel):
    kind: str
    name: str
    desired: int | None = None
    ready: int | None = None
    updated: int | None = None
    available: int | None = None
    images: list[str] = Field(default_factory=list)
    age: str | None = None
    problem: str | None = None


class ReplicaCounts(OutputModel):
    desired: int | None = None
    ready: int | None = None
    updated: int | None = None
    available: int | None = None


class ConditionSummary(OutputModel):
    """A Kubernetes/Argo condition, narrowed to its catalog fields.

    `extra="ignore"` (via `OutputModel`) drops anything else a raw condition
    object carries; conditions are status structs, not a place to smuggle
    arbitrary data through.
    """

    type: str | None = None
    status: str | None = None
    reason: str | None = None
    message: str | None = None
    last_transition_time: str | None = None


class ProbeSummary(OutputModel):
    """A liveness/readiness/startup probe, narrowed to the fields that explain
    *why* a probe fails. Deliberately excludes `httpGet.httpHeaders` (can
    carry bearer tokens) and `exec.command` (can carry secrets as CLI args
    or embed `env[].value`-style content) - see AGENTS.md invariant 2.
    """

    type: Literal["http", "tcp", "exec", "grpc"] | None = None
    path: str | None = None
    port: str | None = None
    period_seconds: int | None = None
    timeout_seconds: int | None = None
    failure_threshold: int | None = None
    success_threshold: int | None = None
    initial_delay_seconds: int | None = None

    @model_validator(mode="before")
    @classmethod
    def summarize_probe(cls, payload: Any) -> Any:
        if not isinstance(payload, dict):
            return payload
        summary: dict[str, Any] = dict(cast(dict[str, Any], payload))
        for raw_kind, kind in (
            ("httpGet", "http"),
            ("tcpSocket", "tcp"),
            ("grpc", "grpc"),
            ("exec", "exec"),
        ):
            action = summary.get(raw_kind)
            if isinstance(action, dict):
                action = cast(dict[str, Any], action)
                summary["type"] = kind
                if kind == "http":
                    summary["path"] = action.get("path")
                if "port" in action:
                    summary["port"] = str(action["port"])
                break
        for raw_key, key in (
            ("periodSeconds", "period_seconds"),
            ("timeoutSeconds", "timeout_seconds"),
            ("failureThreshold", "failure_threshold"),
            ("successThreshold", "success_threshold"),
            ("initialDelaySeconds", "initial_delay_seconds"),
        ):
            if raw_key in summary:
                summary[key] = summary[raw_key]
        return summary


class ContainerSummary(OutputModel):
    name: str
    image: str | None = None
    requests: dict[str, str] = Field(default_factory=dict)
    limits: dict[str, str] = Field(default_factory=dict)
    env_names: list[str] = Field(default_factory=list)
    probes: dict[str, ProbeSummary] | None = None


class HpaSummary(OutputModel):
    min: int | None = None
    max: int | None = None
    current: int | None = None
    desired: int | None = None


class PreviousReplicaSet(OutputModel):
    name: str | None = None
    revision: str | None = None
    images: list[str] = Field(default_factory=list)


class WorkloadDetails(OutputModel):
    replicas: ReplicaCounts = Field(default_factory=ReplicaCounts)
    strategy: str | None = None
    revision: str | None = None
    containers: list[ContainerSummary] = Field(default_factory=list)
    conditions: list[ConditionSummary] = Field(default_factory=list)
    current_replicaset: str | None = None
    previous_replicaset: PreviousReplicaSet | None = None
    hpa: HpaSummary | None = None


class LastTermination(OutputModel):
    reason: str | None = None
    exit_code: int | None = None
    finished_at: str | None = None


class PodSummary(OutputModel):
    name: str
    phase: str | None = None
    ready: str | None = None
    restarts: int = 0
    last_termination: LastTermination | None = None
    waiting_reason: str | None = None
    node: str | None = None
    age: str | None = None
    images: list[str] = Field(default_factory=list)


class EnvReference(OutputModel):
    name: str
    value_from: dict[str, Any] | None = None


class EventSummary(OutputModel):
    last_seen: str | None = None
    type: str | None = None
    reason: str | None = None
    object: str | None = None
    count: int | None = None
    message: str | None = None


class PodContainerDetails(OutputModel):
    name: str
    image: str | None = None
    ready: bool | None = None
    restart_count: int = 0
    state: dict[str, Any] | None = None
    last_state: dict[str, Any] | None = None
    requests: dict[str, str] = Field(default_factory=dict)
    limits: dict[str, str] = Field(default_factory=dict)
    env: list[EnvReference] = Field(default_factory=list)
    probes: dict[str, ProbeSummary] | None = None


class PodDescription(OutputModel):
    phase: str | None = None
    node: str | None = None
    qos_class: str | None = None
    start_time: str | None = None
    conditions: list[ConditionSummary] = Field(default_factory=list)
    containers: list[PodContainerDetails] = Field(default_factory=list)
    init_containers: list[PodContainerDetails] = Field(default_factory=list)
    events: list[EventSummary] = Field(default_factory=list)


class PodLogs(OutputModel):
    container: str | None = None
    lines: list[str] = Field(default_factory=list)
    truncated: bool = False


class ServiceSummary(OutputModel):
    name: str
    type: str | None = None
    cluster_ip: str | None = None
    ports: list[dict[str, Any]] = Field(default_factory=list)
    selector: dict[str, str] = Field(default_factory=dict)
    ready_endpoints: int = 0
    not_ready_endpoints: int = 0


class IngressSummary(OutputModel):
    name: str
    class_: str | None = Field(default=None, alias="class")
    hosts: list[str] = Field(default_factory=list)
    rules: list[dict[str, Any]] = Field(default_factory=list)
    load_balancer_hostname: str | None = None
    tls_hosts: list[str] = Field(default_factory=list)
    annotations: dict[str, str] = Field(default_factory=dict)


class HpaDetails(OutputModel):
    name: str
    target: str | None = None
    min: int | None = None
    max: int | None = None
    current: int | None = None
    desired: int | None = None
    metrics: list[dict[str, Any]] = Field(default_factory=list)
    conditions: list[ConditionSummary] = Field(default_factory=list)


class NodeSummary(OutputModel):
    name: str
    ready: bool | None = None
    pressure: list[str] = Field(default_factory=list)
    instance_type: str | None = None
    nodepool: str | None = None
    zone: str | None = None
    capacity: dict[str, str] = Field(default_factory=dict)
    allocatable: dict[str, str] = Field(default_factory=dict)
    taints: list[dict[str, str]] = Field(default_factory=list)
    age: str | None = None
    kubelet_version: str | None = None


class TopPodSummary(OutputModel):
    pod: str
    container: str
    cpu_millicores: float | None = None
    memory_mib: float | None = None
    cpu_request: str | None = None
    memory_request: str | None = None
    memory_limit: str | None = None


class NodeclaimSummary(OutputModel):
    name: str
    nodepool: str | None = None
    instance_type: str | None = None
    capacity_type: str | None = None
    zone: str | None = None
    ready: bool | None = None
    launched_at: str | None = None
    conditions: list[ConditionSummary] = Field(default_factory=list)


class ApplicationSummary(OutputModel):
    name: str
    project: str | None = None
    destination_namespace: str | None = None
    sync: str | None = None
    health: str | None = None
    revision_short: str | None = None
    last_sync: dict[str, Any] | None = None


class OperationState(OutputModel):
    phase: str | None = None
    message: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class UnhealthyResource(OutputModel):
    kind: str | None = None
    namespace: str | None = None
    name: str | None = None
    sync: str | None = None
    health: str | None = None
    message: str | None = None


class AppHistoryEntry(OutputModel):
    revision_short: str | None = None
    deployed_at: str | None = None


class ApplicationDetails(ApplicationSummary):
    # Status-only contract (ADR-007, AGENTS.md invariant 2): Argo CD Application
    # `spec` (which can carry inline Helm values) is never modeled or returned,
    # even in redacted form. Do not re-add a `source` field here.
    conditions: list[ConditionSummary] = Field(default_factory=list)
    operation_state: OperationState | None = None
    unhealthy_resources: list[UnhealthyResource] = Field(default_factory=list)
    history: list[AppHistoryEntry] = Field(default_factory=list)


class RolloutDetails(OutputModel):
    phase: str | None = None
    message: str | None = None
    strategy: str | None = None
    current_step: str | None = None
    set_weight: int | None = None
    paused: dict[str, Any] | None = None
    aborted: bool = False
    stable_rs: dict[str, Any] | None = None
    canary_rs: dict[str, Any] | None = None
    replicas: ReplicaCounts = Field(default_factory=ReplicaCounts)
    current_analysis_runs: list[str] = Field(default_factory=list)


class AnalysisRunSummary(OutputModel):
    name: str
    phase: str | None = None
    started_at: str | None = None
    message: str | None = None
    metrics: list[dict[str, Any]] = Field(default_factory=list)


class AddonSummary(OutputModel):
    name: str
    version: str | None = None
    status: str | None = None
    health_issues: list[dict[str, Any]] = Field(default_factory=list)


class EksClusterDetails(OutputModel):
    name: str
    version: str | None = None
    platform_version: str | None = None
    status: str | None = None
    endpoint_public_access: bool | None = None
    endpoint_private_access: bool | None = None
    addons: list[AddonSummary] = Field(default_factory=list)


class NodegroupSummary(OutputModel):
    name: str
    status: str | None = None
    version: str | None = None
    release_version: str | None = None
    instance_types: list[str] = Field(default_factory=list)
    capacity_type: str | None = None
    scaling: dict[str, int] = Field(default_factory=dict)
    health_issues: list[dict[str, Any]] = Field(default_factory=list)


class AlbTarget(OutputModel):
    id: str
    port: int | None = None
    state: str | None = None
    reason: str | None = None
    description: str | None = None


class AlbTargetGroup(OutputModel):
    name: str
    port: int | None = None
    protocol: str | None = None
    health_check_path: str | None = None
    targets: list[AlbTarget] = Field(default_factory=list)


class AlbDetails(OutputModel):
    lb_name: str
    scheme: str | None = None
    state: str | None = None
    target_groups: list[AlbTargetGroup] = Field(default_factory=list)


class MetricPoint(OutputModel):
    t: str
    v: float


class MetricSummary(OutputModel):
    min: float | None = None
    max: float | None = None
    avg: float | None = None
    last: float | None = None


class AlbMetricDetails(OutputModel):
    metric: str
    unit: str | None = None
    points: list[MetricPoint] = Field(default_factory=list)
    summary: MetricSummary = Field(default_factory=MetricSummary)


class ListNamespacesOutput(RootModel[list[NamespaceSummary]]):
    pass


class ListWorkloadsOutput(RootModel[list[WorkloadSummary]]):
    pass


class GetWorkloadOutput(WorkloadDetails):
    pass


class GetPodsOutput(RootModel[list[PodSummary]]):
    pass


class DescribePodOutput(PodDescription):
    pass


class GetEventsOutput(RootModel[list[EventSummary]]):
    pass


class GetPodLogsOutput(PodLogs):
    pass


class ListServicesOutput(RootModel[list[ServiceSummary]]):
    pass


class ListIngressesOutput(RootModel[list[IngressSummary]]):
    pass


class ListHpasOutput(RootModel[list[HpaDetails]]):
    pass


class GetNodesOutput(RootModel[list[NodeSummary]]):
    pass


class TopPodsOutput(RootModel[list[TopPodSummary]]):
    pass


class ListNodeclaimsOutput(RootModel[list[NodeclaimSummary]]):
    pass


class ListApplicationsOutput(RootModel[list[ApplicationSummary]]):
    pass


class GetApplicationOutput(ApplicationDetails):
    pass


class GetRolloutOutput(RolloutDetails):
    pass


class ListAnalysisRunsOutput(RootModel[list[AnalysisRunSummary]]):
    pass


class GetEksClusterOutput(EksClusterDetails):
    pass


class ListNodegroupsOutput(RootModel[list[NodegroupSummary]]):
    pass


class GetAlbForIngressOutput(AlbDetails):
    pass


class GetAlbMetricsOutput(RootModel[list[AlbMetricDetails]]):
    pass


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    args_model: type[ToolModel]
    output_model: type[BaseModel]
    description: str
    capability: str | None = None
    namespaced: bool = False
    hub_local: bool = False


TOOL_REGISTRY: dict[str, ToolDefinition] = {
    "list_clusters": ToolDefinition(
        "list_clusters",
        EmptyArgs,
        ListClustersOutput,
        "List clusters available in this Slack channel.",
        hub_local=True,
    ),
    "list_namespaces": ToolDefinition(
        "list_namespaces",
        ListNamespacesArgs,
        ListNamespacesOutput,
        "List namespaces visible to pro-pilot on the cluster.",
        "k8s",
    ),
    "list_workloads": ToolDefinition(
        "list_workloads",
        ListWorkloadsArgs,
        ListWorkloadsOutput,
        "List workloads and readiness problems in a namespace.",
        "k8s",
        True,
    ),
    "get_workload": ToolDefinition(
        "get_workload",
        GetWorkloadArgs,
        GetWorkloadOutput,
        "Inspect replicas, containers, conditions, and HPA for one workload.",
        "k8s",
        True,
    ),
    "get_pods": ToolDefinition(
        "get_pods",
        GetPodsArgs,
        GetPodsOutput,
        "List pod readiness, restarts, images, and termination reasons.",
        "k8s",
        True,
    ),
    "describe_pod": ToolDefinition(
        "describe_pod",
        DescribePodArgs,
        DescribePodOutput,
        "Inspect one pod's safe projected spec, states, and recent events.",
        "k8s",
        True,
    ),
    "get_events": ToolDefinition(
        "get_events",
        GetEventsArgs,
        GetEventsOutput,
        "List recent Kubernetes events for a namespace or object.",
        "k8s",
        True,
    ),
    "get_pod_logs": ToolDefinition(
        "get_pod_logs",
        GetPodLogsArgs,
        GetPodLogsOutput,
        "Read capped, redacted pod logs; use previous for crash loops.",
        "k8s",
        True,
    ),
    "list_services": ToolDefinition(
        "list_services",
        ListServicesArgs,
        ListServicesOutput,
        "List Services and ready endpoint counts in a namespace.",
        "k8s",
        True,
    ),
    "list_ingresses": ToolDefinition(
        "list_ingresses",
        ListIngressesArgs,
        ListIngressesOutput,
        "List ingress hosts, routes, and load balancer hostnames.",
        "k8s",
        True,
    ),
    "list_hpas": ToolDefinition(
        "list_hpas",
        ListHpasArgs,
        ListHpasOutput,
        "List HPA targets, replica limits, metrics, and conditions.",
        "k8s",
        True,
    ),
    "get_nodes": ToolDefinition(
        "get_nodes",
        GetNodesArgs,
        GetNodesOutput,
        "List node readiness, pressure, capacity, and versions.",
        "k8s",
    ),
    "top_pods": ToolDefinition(
        "top_pods",
        TopPodsArgs,
        TopPodsOutput,
        "Show pods and containers sorted by memory usage.",
        "metrics",
        True,
    ),
    "list_nodeclaims": ToolDefinition(
        "list_nodeclaims",
        ListNodeclaimsArgs,
        ListNodeclaimsOutput,
        "List Karpenter NodeClaims and non-healthy conditions.",
        "karpenter",
    ),
    "list_applications": ToolDefinition(
        "list_applications",
        ListApplicationsArgs,
        ListApplicationsOutput,
        "List Argo CD applications targeting visible namespaces.",
        "argocd",
    ),
    "get_application": ToolDefinition(
        "get_application",
        GetApplicationArgs,
        GetApplicationOutput,
        "Inspect Argo CD sync, health, source, operation, and history.",
        "argocd",
    ),
    "get_rollout": ToolDefinition(
        "get_rollout",
        GetRolloutArgs,
        GetRolloutOutput,
        "Inspect rollout strategy, pause state, replicas, and analyses.",
        "rollouts",
        True,
    ),
    "list_analysis_runs": ToolDefinition(
        "list_analysis_runs",
        ListAnalysisRunsArgs,
        ListAnalysisRunsOutput,
        "List recent Argo Rollouts analysis runs and metrics.",
        "rollouts",
        True,
    ),
    "get_eks_cluster": ToolDefinition(
        "get_eks_cluster",
        EmptyArgs,
        GetEksClusterOutput,
        "Inspect safe EKS version, status, access mode, and add-ons.",
        "aws",
    ),
    "list_nodegroups": ToolDefinition(
        "list_nodegroups",
        EmptyArgs,
        ListNodegroupsOutput,
        "List EKS node groups, scaling, versions, and health issues.",
        "aws",
    ),
    "get_alb_for_ingress": ToolDefinition(
        "get_alb_for_ingress",
        GetAlbForIngressArgs,
        GetAlbForIngressOutput,
        "Inspect ALB target health for an ingress.",
        "aws",
        True,
    ),
    "get_alb_metrics": ToolDefinition(
        "get_alb_metrics",
        GetAlbMetricsArgs,
        GetAlbMetricsOutput,
        "Read capped ALB request, error, latency, or health metrics.",
        "aws",
        True,
    ),
}


def tool_schema(tool: ToolDefinition, cluster_ids: list[str] | None = None) -> dict[str, Any]:
    """Return the shared tool argument schema, optionally adding channel cluster scope."""

    schema = tool.args_model.model_json_schema()
    if cluster_ids is None or tool.hub_local:
        return schema
    properties = dict(schema.get("properties", {}))
    properties = {"cluster": {"type": "string", "enum": cluster_ids}, **properties}
    required = ["cluster", *schema.get("required", [])]
    return {**schema, "properties": properties, "required": required}


def bedrock_tool_specs(
    tool_names: list[str] | None = None,
    cluster_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Export registry entries in the Bedrock Converse ``toolConfig`` shape."""

    names = list(TOOL_REGISTRY) if tool_names is None else tool_names
    return [
        {
            "toolSpec": {
                "name": TOOL_REGISTRY[name].name,
                "description": TOOL_REGISTRY[name].description,
                "inputSchema": {"json": tool_schema(TOOL_REGISTRY[name], cluster_ids)},
            }
        }
        for name in names
        if name in TOOL_REGISTRY
    ]
