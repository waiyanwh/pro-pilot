"""Synchronous, read-only Kubernetes catalogue executors."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from kubernetes.utils.quantity import parse_quantity

from pro_pilot.spoke.execution import mark_truncated
from pro_pilot.spoke.policy import NamespacePolicy
from pro_pilot.spoke.tools.common import (
    KubeReader,
    age,
    conditions,
    images,
    owned_by,
    parse_time,
    selector,
    timestamp,
    utc_now,
)

_WORKLOADS = {
    "deployment": ("apps/v1", "deployments", "Deployment"),
    "statefulset": ("apps/v1", "statefulsets", "StatefulSet"),
    "daemonset": ("apps/v1", "daemonsets", "DaemonSet"),
    "job": ("batch/v1", "jobs", "Job"),
    "cronjob": ("batch/v1", "cronjobs", "CronJob"),
}
_TOOLS = frozenset(
    {
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
    }
)
_INGRESS_ANNOTATIONS = (
    "alb.ingress.kubernetes.io/group.name",
    "alb.ingress.kubernetes.io/scheme",
    "alb.ingress.kubernetes.io/healthcheck-path",
)


def _name(obj: dict[str, Any]) -> str:
    return obj.get("metadata", {}).get("name", "")


def _resources(container: dict[str, Any], key: str) -> dict[str, str]:
    return {name: str(value) for name, value in container.get("resources", {}).get(key, {}).items()}


def _probes(container: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for raw, name in (
        ("livenessProbe", "liveness"),
        ("readinessProbe", "readiness"),
        ("startupProbe", "startup"),
    ):
        probe = container.get(raw)
        if not isinstance(probe, dict):
            continue
        summary: dict[str, Any] = {}
        for action, kind in (
            ("httpGet", "http"),
            ("tcpSocket", "tcp"),
            ("grpc", "grpc"),
            ("exec", "exec"),
        ):
            if action in probe:
                summary["type"] = kind
                if kind == "http":
                    summary["path"] = probe[action].get("path")
                if "port" in probe[action]:
                    summary["port"] = str(probe[action]["port"])
                break
        for raw_key, key in (
            ("periodSeconds", "period_seconds"),
            ("timeoutSeconds", "timeout_seconds"),
            ("failureThreshold", "failure_threshold"),
            ("successThreshold", "success_threshold"),
            ("initialDelaySeconds", "initial_delay_seconds"),
        ):
            if raw_key in probe:
                summary[key] = probe[raw_key]
        result[name] = summary
    return result


def _counts(obj: dict[str, Any], kind: str) -> dict[str, Any]:
    spec, status = obj.get("spec", {}), obj.get("status", {})
    if kind == "daemonset":
        return dict(
            desired=status.get("desiredNumberScheduled", 0),
            ready=status.get("numberReady", 0),
            updated=status.get("updatedNumberScheduled", 0),
            available=status.get("numberAvailable", 0),
        )
    if kind == "job":
        return dict(
            desired=spec.get("completions", 1),
            ready=status.get("succeeded", 0),
            updated=status.get("active", 0),
            available=status.get("succeeded", 0),
        )
    if kind == "cronjob":
        return dict(
            desired=0 if spec.get("suspend") else 1,
            ready=len(status.get("active", [])),
            updated=None,
            available=None,
        )
    return dict(
        desired=spec.get("replicas", 1),
        ready=status.get("readyReplicas", 0),
        updated=status.get("updatedReplicas", 0),
        available=status.get("availableReplicas", 0),
    )


def _state(value: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for kind in ("waiting", "running", "terminated"):
        if kind not in value:
            continue
        row = value[kind]
        detail = {key: row[key] for key in ("reason", "message") if key in row}
        for raw, key in (
            ("exitCode", "exit_code"),
            ("signal", "signal"),
            ("startedAt", "started_at"),
            ("finishedAt", "finished_at"),
        ):
            if raw in row:
                detail[key] = timestamp(row[raw]) if raw.endswith("At") else row[raw]
        result[kind] = detail
    return result


def _env_reference(value: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for kind, keys in (
        ("secretKeyRef", ("name", "key", "optional")),
        ("configMapKeyRef", ("name", "key", "optional")),
        ("fieldRef", ("apiVersion", "fieldPath")),
        ("resourceFieldRef", ("containerName", "resource", "divisor")),
    ):
        if kind in value:
            result[kind] = {key: value[kind][key] for key in keys if key in value[kind]}
    return result


def _event_time(row: dict[str, Any]) -> Any:
    return (
        row.get("series", {}).get("lastObservedTime")
        or row.get("lastTimestamp")
        or row.get("eventTime")
        or row.get("firstTimestamp")
        or row.get("metadata", {}).get("creationTimestamp")
    )


def _hpa_counts(row: dict[str, Any]) -> dict[str, Any]:
    spec, status = row.get("spec", {}), row.get("status", {})
    return dict(
        min=spec.get("minReplicas", 1),
        max=spec.get("maxReplicas"),
        current=status.get("currentReplicas"),
        desired=status.get("desiredReplicas"),
    )


class K8sTools:
    def __init__(
        self,
        reader: KubeReader,
        policy: NamespacePolicy,
        *,
        max_log_lines: int = 200,
        max_log_since_s: int = 3600,
    ) -> None:
        self.reader = reader
        self.policy = policy
        self.max_log_lines = max_log_lines
        self.max_log_since_s = max_log_since_s

    def execute(self, tool_name: str, args: dict[str, Any]) -> Any:
        if tool_name not in _TOOLS:
            raise ValueError("unknown Kubernetes tool")
        namespace = args.get("namespace")
        if namespace is not None and not self.policy.allowed(namespace):
            raise PermissionError(self.policy.denied_message(namespace))
        return getattr(self, tool_name)(args)

    def _list(
        self,
        version: str,
        resource: str,
        namespace: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if namespace is not None and not self.policy.allowed(namespace):
            raise PermissionError(self.policy.denied_message(namespace))
        rows = self.reader.list(version, resource, namespace=namespace, params=params)
        if namespace is None:
            return rows
        return [
            row for row in rows if row.get("metadata", {}).get("namespace", namespace) == namespace
        ]

    def _workload(self, namespace: str, ref: dict[str, Any]) -> dict[str, Any]:
        version, resource, _ = _WORKLOADS[ref["kind"]]
        return self.reader.get(version, resource, namespace=namespace, name=ref["name"])

    def _pods(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        namespace, ref = args["namespace"], args.get("workload")
        query = args.get("label_selector")
        owners: set[tuple[str, str]] | None = None
        if ref:
            workload = self._workload(namespace, ref)
            kind = ref["kind"]
            owners = {(_WORKLOADS[kind][2], ref["name"])}
            if kind == "cronjob":
                jobs = [
                    row
                    for row in self._list("batch/v1", "jobs", namespace)
                    if owned_by(row, kind="CronJob", name=ref["name"])
                ]
                # CronJobs have no pod selector. Only follow owned Jobs with real selectors.
                owned_pods: dict[str, dict[str, Any]] = {}
                for job in jobs:
                    job_selector = selector(job.get("spec", {}).get("selector", {}))
                    if not job_selector:
                        continue
                    params = {"labelSelector": ",".join(filter(None, (job_selector, query)))}
                    for pod in self._list("v1", "pods", namespace, params):
                        if owned_by(pod, kind="Job", name=_name(job)):
                            owned_pods[_name(pod)] = pod
                return list(owned_pods.values())
            workload_selector = selector(workload.get("spec", {}).get("selector", {}))
            if not workload_selector:
                return []
            query = ",".join(filter(None, (workload_selector, query)))
            if kind == "deployment":
                replica_sets = self._list(
                    "apps/v1", "replicasets", namespace, {"labelSelector": workload_selector}
                )
                owners.update(
                    ("ReplicaSet", _name(row))
                    for row in replica_sets
                    if owned_by(row, kind="Deployment", name=ref["name"])
                )
        pods = self._list("v1", "pods", namespace, {"labelSelector": query} if query else None)
        if owners is not None:
            pods = [
                pod
                for pod in pods
                if any(owned_by(pod, kind=kind, name=name) for kind, name in owners)
            ]
        return pods

    def list_namespaces(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "name": _name(row),
                "phase": row.get("status", {}).get("phase"),
                "age": age(row.get("metadata", {}).get("creationTimestamp")),
            }
            for row in self._list("v1", "namespaces")
            if self.policy.allowed(_name(row)) and (args.get("name_contains") or "") in _name(row)
        ]

    def list_workloads(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        kinds = (
            [args["kind"]]
            if args.get("kind")
            else ["deployment", "statefulset", "daemonset", "cronjob"]
        )
        for kind in kinds:
            version, resource, _ = _WORKLOADS[kind]
            for row in self._list(version, resource, args["namespace"]):
                if (args.get("name_contains") or "") not in _name(row):
                    continue
                template = row.get("spec", {}).get("template", {})
                if kind == "cronjob":
                    template = (
                        row.get("spec", {})
                        .get("jobTemplate", {})
                        .get("spec", {})
                        .get("template", {})
                    )
                problem = next(
                    (
                        c.get("message") or c.get("reason")
                        for c in row.get("status", {}).get("conditions", [])
                        if (
                            c.get("status") != "True"
                            and c.get("type") in {"Available", "Progressing", "Ready", "Complete"}
                        )
                        or (
                            c.get("status") == "True"
                            and c.get("type") in {"ReplicaFailure", "Failed"}
                        )
                    ),
                    None,
                )
                result.append(
                    {
                        "kind": kind,
                        "name": _name(row),
                        **_counts(row, kind),
                        "images": images(template),
                        "age": age(row.get("metadata", {}).get("creationTimestamp")),
                        "problem": problem,
                    }
                )
        return result

    def get_workload(self, args: dict[str, Any]) -> dict[str, Any]:
        row = self._workload(args["namespace"], args)
        spec, status = row.get("spec", {}), row.get("status", {})
        revision = (
            row.get("metadata", {}).get("annotations", {}).get("deployment.kubernetes.io/revision")
        )
        result = {
            "replicas": _counts(row, args["kind"]),
            "strategy": spec.get("strategy", spec.get("updateStrategy", {})).get("type"),
            "revision": revision,
            "conditions": conditions(status.get("conditions", [])),
            "containers": [
                {
                    "name": c["name"],
                    "image": c.get("image"),
                    "requests": _resources(c, "requests"),
                    "limits": _resources(c, "limits"),
                    "env_names": [e["name"] for e in c.get("env", [])],
                    "probes": _probes(c),
                }
                for c in spec.get("template", {}).get("spec", {}).get("containers", [])
            ],
            "current_replicaset": None,
            "previous_replicaset": None,
            "hpa": None,
        }
        if args["kind"] == "deployment":
            sets = [
                r
                for r in self._list("apps/v1", "replicasets", args["namespace"])
                if owned_by(r, kind="Deployment", name=args["name"])
            ]

            def rs_revision(rs: dict[str, Any]) -> int:
                value = (
                    rs.get("metadata", {})
                    .get("annotations", {})
                    .get("deployment.kubernetes.io/revision", "0")
                )
                return int(value) if str(value).isdigit() else 0

            sets.sort(key=rs_revision, reverse=True)
            current = next((rs for rs in sets if str(rs_revision(rs)) == str(revision)), None)
            if current is None:
                # Match templates while ignoring the controller-injected hash label.
                template = spec.get("template", {})
                current = next(
                    (
                        rs
                        for rs in sets
                        if rs.get("spec", {}).get("template", {}).get("spec")
                        == template.get("spec")
                    ),
                    None,
                )
            if current:
                result["current_replicaset"] = _name(current)
                previous = next((rs for rs in sets if rs_revision(rs) < rs_revision(current)), None)
                if previous:
                    result["previous_replicaset"] = {
                        "name": _name(previous),
                        "revision": str(rs_revision(previous)),
                        "images": images(previous.get("spec", {}).get("template", {})),
                    }
        for hpa in self._list("autoscaling/v2", "horizontalpodautoscalers", args["namespace"]):
            target = hpa.get("spec", {}).get("scaleTargetRef", {})
            if (
                target.get("kind") == _WORKLOADS[args["kind"]][2]
                and target.get("name") == args["name"]
            ):
                result["hpa"] = _hpa_counts(hpa)
                break
        return result

    def get_pods(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for pod in self._pods(args):
            spec, status = pod.get("spec", {}), pod.get("status", {})
            statuses = status.get("containerStatuses", [])
            all_statuses = statuses + status.get("initContainerStatuses", [])
            ready = sum(bool(c.get("ready")) for c in statuses)
            total = len(spec.get("containers", []))
            restarts = sum(c.get("restartCount", 0) for c in all_statuses)
            mode = args.get("status", "all")
            if (
                (mode == "pending" and status.get("phase") != "Pending")
                or (mode == "not_ready" and ready == total)
                or (mode == "restarting" and restarts == 0)
            ):
                continue
            terminations = [c.get("lastState", {}).get("terminated") for c in all_statuses]
            terminated = max(
                (t for t in terminations if t),
                key=lambda t: timestamp(t.get("finishedAt")) or "",
                default=None,
            )
            result.append(
                {
                    "name": _name(pod),
                    "phase": status.get("phase"),
                    "ready": f"{ready}/{total}",
                    "restarts": restarts,
                    "last_termination": {
                        "reason": terminated.get("reason"),
                        "exit_code": terminated.get("exitCode"),
                        "finished_at": timestamp(terminated.get("finishedAt")),
                    }
                    if terminated
                    else None,
                    "waiting_reason": next(
                        (
                            c["state"]["waiting"].get("reason")
                            for c in all_statuses
                            if "waiting" in c.get("state", {})
                        ),
                        None,
                    ),
                    "node": spec.get("nodeName"),
                    "age": age(pod.get("metadata", {}).get("creationTimestamp")),
                    "images": images(pod),
                }
            )
        return result

    def describe_pod(self, args: dict[str, Any]) -> dict[str, Any]:
        pod = self.reader.get("v1", "pods", namespace=args["namespace"], name=args["name"])
        spec, status = pod.get("spec", {}), pod.get("status", {})

        def containers(key: str, status_key: str) -> list[dict[str, Any]]:
            by_name = {row["name"]: row for row in status.get(status_key, [])}
            result: list[dict[str, Any]] = []
            for c in spec.get(key, []):
                s = by_name.get(c["name"], {})
                result.append(
                    {
                        "name": c["name"],
                        "image": c.get("image"),
                        "ready": s.get("ready"),
                        "restart_count": s.get("restartCount", 0),
                        "state": _state(s.get("state", {})),
                        "last_state": _state(s.get("lastState", {})),
                        "requests": _resources(c, "requests"),
                        "limits": _resources(c, "limits"),
                        "probes": _probes(c),
                        "env": [
                            {
                                "name": e["name"],
                                **(
                                    {"value_from": _env_reference(e["valueFrom"])}
                                    if "valueFrom" in e
                                    else {}
                                ),
                            }
                            for e in c.get("env", [])
                        ],
                    }
                )
            return result

        event_args = {
            "namespace": args["namespace"],
            "involved_kind": "Pod",
            "involved_name": args["name"],
            "type": "all",
        }
        events = self._events(event_args, uid=pod.get("metadata", {}).get("uid"))
        if len(events) > 20:
            mark_truncated()
        return {
            "phase": status.get("phase"),
            "node": spec.get("nodeName"),
            "qos_class": status.get("qosClass"),
            "start_time": timestamp(status.get("startTime")),
            "conditions": conditions(status.get("conditions", [])),
            "containers": containers("containers", "containerStatuses"),
            "init_containers": containers("initContainers", "initContainerStatuses"),
            "events": events[:20],
        }

    def _events(self, args: dict[str, Any], *, uid: str | None = None) -> list[dict[str, Any]]:
        filters = []
        for key, field in (
            ("involved_kind", "involvedObject.kind"),
            ("involved_name", "involvedObject.name"),
        ):
            if args.get(key):
                filters.append(f"{field}={args[key]}")
        if uid:
            filters.append(f"involvedObject.uid={uid}")
        if args.get("type", "Warning") != "all":
            filters.append(f"type={args.get('type', 'Warning')}")
        rows = self._list(
            "v1",
            "events",
            args["namespace"],
            {"fieldSelector": ",".join(filters)} if filters else None,
        )
        cutoff = (
            utc_now() - timedelta(minutes=args["since_minutes"])
            if "since_minutes" in args
            else None
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            involved = row.get("involvedObject", row.get("regarding", {}))
            if (
                args.get("involved_kind")
                and involved.get("kind") != args["involved_kind"]
                or args.get("involved_name")
                and involved.get("name") != args["involved_name"]
                or uid
                and involved.get("uid") != uid
                or args.get("type", "Warning") != "all"
                and row.get("type") != args.get("type", "Warning")
            ):
                continue
            involved_ns = involved.get("namespace")
            if involved_ns and not self.policy.allowed(involved_ns):
                continue
            seen = parse_time(_event_time(row))
            if cutoff and (seen is None or seen < cutoff):
                continue
            result.append(
                {
                    "last_seen": timestamp(_event_time(row)),
                    "type": row.get("type"),
                    "reason": row.get("reason"),
                    "object": f"{involved.get('kind', '')}/{involved.get('name', '')}",
                    "count": row.get("series", {}).get("count", row.get("count", 1)),
                    "message": row.get("message", row.get("note")),
                }
            )
        result.sort(key=lambda row: row["last_seen"] or "", reverse=True)
        return result

    def get_events(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        return self._events({"since_minutes": 60, **args})

    def get_pod_logs(self, args: dict[str, Any]) -> dict[str, Any]:
        pod = self.reader.get("v1", "pods", namespace=args["namespace"], name=args["pod"])
        spec = pod.get("spec", {})
        normal = [c["name"] for c in spec.get("containers", [])]
        names = normal + [
            c["name"]
            for key in ("initContainers", "ephemeralContainers")
            for c in spec.get(key, [])
        ]
        container = args.get("container")
        if container is None:
            if len(normal) != 1:
                raise ValueError("container must be specified for a multi-container pod")
            container = normal[0]
        if container not in names:
            raise ValueError("container does not exist in the pod")
        tail = min(args.get("tail_lines", 100), self.max_log_lines)
        since = min(args.get("since_minutes", 15) * 60, self.max_log_since_s)
        capped = tail < args.get("tail_lines", 100) or since < args.get("since_minutes", 15) * 60
        text = self.reader.get(
            "v1",
            "pods",
            namespace=args["namespace"],
            name=args["pod"],
            subresource="log",
            params={
                "container": container,
                "tailLines": tail,
                "sinceSeconds": since,
                "previous": args.get("previous", False),
                "timestamps": False,
            },
        )
        lines = text.splitlines()
        # A full tail may omit earlier lines; conservatively advertise that bound.
        capped = capped or len(lines) >= tail
        if len(lines) > tail:
            lines = lines[-tail:]
        contains = args.get("contains")
        if contains:
            lines = [line for line in lines if contains.casefold() in line.casefold()]
        if capped:
            mark_truncated()
        # Redaction and the 1000-character line cap run in the common pipeline, in that order.
        return {"container": container, "lines": lines, "truncated": capped}

    def list_services(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        endpoints = {_name(row): row for row in self._list("v1", "endpoints", args["namespace"])}
        result: list[dict[str, Any]] = []
        for row in self._list("v1", "services", args["namespace"]):
            spec = row.get("spec", {})
            subsets = endpoints.get(_name(row), {}).get("subsets", [])
            result.append(
                {
                    "name": _name(row),
                    "type": spec.get("type"),
                    "cluster_ip": spec.get("clusterIP"),
                    "ports": [
                        {
                            key: port[key]
                            for key in (
                                "name",
                                "protocol",
                                "port",
                                "targetPort",
                                "nodePort",
                                "appProtocol",
                            )
                            if key in port
                        }
                        for port in spec.get("ports", [])
                    ],
                    "selector": spec.get("selector", {}),
                    "ready_endpoints": sum(len(s.get("addresses", [])) for s in subsets),
                    "not_ready_endpoints": sum(
                        len(s.get("notReadyAddresses", [])) for s in subsets
                    ),
                }
            )
        return result

    def list_ingresses(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for row in self._list("networking.k8s.io/v1", "ingresses", args["namespace"]):
            spec = row.get("spec", {})
            rules = []
            for rule in spec.get("rules", []):
                for path in rule.get("http", {}).get("paths", []):
                    backend = path.get("backend", {}).get("service", {})
                    rules.append(
                        {
                            "host": rule.get("host"),
                            "path": path.get("path"),
                            "service": backend.get("name"),
                            "port": backend.get("port", {}).get(
                                "number", backend.get("port", {}).get("name")
                            ),
                        }
                    )
            annotations = row.get("metadata", {}).get("annotations", {})
            result.append(
                {
                    "name": _name(row),
                    "class": spec.get("ingressClassName"),
                    "hosts": [r["host"] for r in spec.get("rules", []) if r.get("host")],
                    "rules": rules,
                    "load_balancer_hostname": next(
                        (
                            i["hostname"]
                            for i in row.get("status", {})
                            .get("loadBalancer", {})
                            .get("ingress", [])
                            if i.get("hostname")
                        ),
                        None,
                    ),
                    "tls_hosts": [h for tls in spec.get("tls", []) for h in tls.get("hosts", [])],
                    "annotations": {
                        key: annotations[key] for key in _INGRESS_ANNOTATIONS if key in annotations
                    },
                }
            )
        return result

    def list_hpas(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for row in self._list("autoscaling/v2", "horizontalpodautoscalers", args["namespace"]):
            spec, status = row.get("spec", {}), row.get("status", {})
            target = spec.get("scaleTargetRef", {})
            metrics = []
            current_metrics = status.get("currentMetrics", [])
            for metric in spec.get("metrics", []):
                raw_type = metric.get("type", "")
                metric_type = raw_type[:1].lower() + raw_type[1:]
                detail = metric.get(metric_type, {})
                name = detail.get("name", detail.get("metric", {}).get("name"))
                current = next(
                    (
                        m.get(metric_type, {}).get("current", {})
                        for m in current_metrics
                        if m.get("type") == metric.get("type")
                        and m.get(metric_type, {}).get(
                            "name", m.get(metric_type, {}).get("metric", {}).get("name")
                        )
                        == name
                        and m.get(metric_type, {}).get("container") == detail.get("container")
                    ),
                    {},
                )

                def measure(value: dict[str, Any]) -> Any:
                    return value.get(
                        "averageUtilization", value.get("averageValue", value.get("value"))
                    )

                metrics.append(
                    {
                        "name": name,
                        "current": measure(current),
                        "target": measure(detail.get("target", {})),
                    }
                )
            result.append(
                {
                    "name": _name(row),
                    "target": f"{target.get('kind', '')}/{target.get('name', '')}",
                    **_hpa_counts(row),
                    "metrics": metrics,
                    "conditions": conditions(status.get("conditions", [])),
                }
            )
        return result

    def get_nodes(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for row in self._list(
            "v1",
            "nodes",
            params={"labelSelector": args["label_selector"]}
            if args.get("label_selector")
            else None,
        ):
            status, metadata = row.get("status", {}), row.get("metadata", {})
            labels = metadata.get("labels", {})
            ready = next(
                (
                    c.get("status") == "True"
                    for c in status.get("conditions", [])
                    if c.get("type") == "Ready"
                ),
                None,
            )
            pressure = [
                c["type"].removesuffix("Pressure")
                for c in status.get("conditions", [])
                if c.get("type") in {"MemoryPressure", "DiskPressure", "PIDPressure"}
                and c.get("status") == "True"
            ]
            if (
                args.get("status") == "not_ready"
                and ready is True
                or args.get("status") == "pressure"
                and not pressure
            ):
                continue
            result.append(
                {
                    "name": _name(row),
                    "ready": ready,
                    "pressure": pressure,
                    "instance_type": labels.get(
                        "node.kubernetes.io/instance-type",
                        labels.get("beta.kubernetes.io/instance-type"),
                    ),
                    "nodepool": labels.get(
                        "karpenter.sh/nodepool", labels.get("eks.amazonaws.com/nodegroup")
                    ),
                    "zone": labels.get(
                        "topology.kubernetes.io/zone",
                        labels.get("failure-domain.beta.kubernetes.io/zone"),
                    ),
                    "capacity": {
                        k: str(v)
                        for k, v in status.get("capacity", {}).items()
                        if k in {"cpu", "memory"}
                    },
                    "allocatable": {
                        k: str(v)
                        for k, v in status.get("allocatable", {}).items()
                        if k in {"cpu", "memory"}
                    },
                    "taints": [
                        {"key": t["key"], "effect": t["effect"]}
                        for t in row.get("spec", {}).get("taints", [])
                    ],
                    "age": age(metadata.get("creationTimestamp")),
                    "kubelet_version": status.get("nodeInfo", {}).get("kubeletVersion"),
                }
            )
        return result

    def top_pods(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        pods = {_name(pod): pod for pod in self._pods(args)}
        if not pods:
            return []
        version = self.reader.api_versions["metrics.k8s.io"]
        result: list[dict[str, Any]] = []
        for metric in self._list(version, "pods", args["namespace"]):
            pod = pods.get(_name(metric))
            if pod is None:
                continue
            containers = {
                c["name"]: c
                for c in pod.get("spec", {}).get("containers", [])
                + pod.get("spec", {}).get("initContainers", [])
            }
            for c in metric.get("containers", []):
                raw = containers.get(c["name"], {})
                requests, limits = _resources(raw, "requests"), _resources(raw, "limits")
                usage = c.get("usage", {})
                result.append(
                    {
                        "pod": _name(metric),
                        "container": c["name"],
                        "cpu_millicores": float(parse_quantity(usage["cpu"]) * Decimal(1000))
                        if "cpu" in usage
                        else None,
                        "memory_mib": float(parse_quantity(usage["memory"]) / Decimal(1024**2))
                        if "memory" in usage
                        else None,
                        "cpu_request": requests.get("cpu"),
                        "memory_request": requests.get("memory"),
                        "memory_limit": limits.get("memory"),
                    }
                )
        result.sort(key=lambda row: row["memory_mib"] or 0, reverse=True)
        return result

    def list_nodeclaims(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        version = self.reader.api_versions["karpenter.sh"]
        pools = {_name(row) for row in self._list(version, "nodepools")}
        params = (
            {"labelSelector": f"karpenter.sh/nodepool={args['nodepool']}"}
            if args.get("nodepool")
            else None
        )
        result: list[dict[str, Any]] = []
        for row in self._list(version, "nodeclaims", params=params):
            metadata, status = row.get("metadata", {}), row.get("status", {})
            labels = metadata.get("labels", {})
            pool = labels.get("karpenter.sh/nodepool")
            if pool is None:
                pool = next(
                    (
                        r.get("name")
                        for r in metadata.get("ownerReferences", [])
                        if r.get("kind") == "NodePool" and r.get("name") in pools
                    ),
                    None,
                )
            if args.get("nodepool") and pool != args["nodepool"]:
                continue
            rows = status.get("conditions", [])
            result.append(
                {
                    "name": _name(row),
                    "nodepool": pool,
                    "instance_type": labels.get("node.kubernetes.io/instance-type"),
                    "capacity_type": labels.get("karpenter.sh/capacity-type"),
                    "zone": labels.get("topology.kubernetes.io/zone"),
                    "ready": next(
                        (c.get("status") == "True" for c in rows if c.get("type") == "Ready"), None
                    ),
                    "launched_at": next(
                        (
                            timestamp(c.get("lastTransitionTime"))
                            for c in rows
                            if c.get("type") == "Launched" and c.get("status") == "True"
                        ),
                        None,
                    ),
                    "conditions": conditions([c for c in rows if c.get("status") != "True"]),
                }
            )
        return result
