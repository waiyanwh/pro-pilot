# Tool Catalogue (v1)

Every tool is **read-only**. Each tool has pydantic `Args` and output models in `pro_pilot/protocol/tools.py`. OpenAI function parameters and Bedrock tool schemas are generated from these same models (AGENTS.md invariant 5). The hub adds a required `cluster` argument to every spoke tool (see [hub.md § Tool exposure](hub.md#tool-exposure)).

Tool descriptions given to the model are written for the model: say **when** to use the tool and what it returns. Keep them under 300 characters.

Common limits, applied by the spoke in [spoke.md](spoke.md): `tool_timeout_s` 15, `max_result_bytes` 32 KiB, `max_list_items` 200. Every `namespace` argument is checked against the spoke policy (`NOT_ALLOWED`).

## Summary

| # | Tool | Capability | Scope | Kubernetes RBAC (`get`/`list`) / AWS IAM |
| --- | --- | --- | --- | --- |
| 0 | `list_clusters` | hub-local | — | — |
| 1 | `list_namespaces` | k8s | cluster | `namespaces` |
| 2 | `list_workloads` | k8s | namespace | `apps/deployments,statefulsets,daemonsets`, `batch/jobs,cronjobs` |
| 3 | `get_workload` | k8s | namespace | as 2 + `apps/replicasets`, `autoscaling/horizontalpodautoscalers` |
| 4 | `get_pods` | k8s | namespace | `pods`, `apps/replicasets` (owner resolution) |
| 5 | `describe_pod` | k8s | namespace | `pods`, `events` |
| 6 | `get_events` | k8s | namespace | `events` |
| 7 | `get_pod_logs` | k8s | namespace | `pods`, `pods/log` |
| 8 | `list_services` | k8s | namespace | `services`, `endpoints` |
| 9 | `list_ingresses` | k8s | namespace | `networking.k8s.io/ingresses` |
| 10 | `list_hpas` | k8s | namespace | `autoscaling/horizontalpodautoscalers` |
| 11 | `get_nodes` | k8s | cluster | `nodes` |
| 12 | `top_pods` | metrics | namespace | `metrics.k8s.io/pods` |
| 13 | `list_nodeclaims` | karpenter | cluster | `karpenter.sh/nodeclaims,nodepools` |
| 14 | `list_applications` | argocd | Argo ns (filtered by destination ns) | `argoproj.io/applications` |
| 15 | `get_application` | argocd | Argo ns (filtered by destination ns) | `argoproj.io/applications` |
| 16 | `get_rollout` | rollouts | namespace | `argoproj.io/rollouts`, `apps/replicasets` |
| 17 | `list_analysis_runs` | rollouts | namespace | `argoproj.io/analysisruns` |
| 18 | `get_eks_cluster` | aws | cluster | `eks:DescribeCluster`, `eks:ListAddons`, `eks:DescribeAddon` |
| 19 | `list_nodegroups` | aws | cluster | `eks:ListNodegroups`, `eks:DescribeNodegroup` |
| 20 | `get_alb_for_ingress` | aws | namespace (via ingress) | `networking.k8s.io/ingresses` + `elasticloadbalancing:DescribeLoadBalancers`, `DescribeTargetGroups`, `DescribeTargetHealth` |
| 21 | `get_alb_metrics` | aws | namespace (via ingress) | as 20 + `cloudwatch:GetMetricData` |

Cluster-scoped tools (1, 11, 13, 18, 19) are not namespace-filtered, except `list_namespaces`, which returns only allowed namespaces. Node, NodeClaim, and EKS data contain no workload payloads.

Phase 1 scope acceptance preserves these contracts: namespaced tools reject denied namespaces before any API read; a direct Application lookup rejects a denied destination, while Application/namespace lists filter denied entries. Cluster-scoped tools reject an injected `namespace` argument as `INVALID_ARGS`, not `NOT_ALLOWED`, and return no namespaced workload payloads. The 21-tool offline matrix covers each declared scope alongside validation, raw projection, redaction and serialized-size limits.

## Definitions

### 0. `list_clusters` (hub)
Args: none. Returns `[{cluster_id, connected, labels, capabilities}]` for the clusters mapped to the current channel. Use it when the user does not name an environment and more than one cluster is mapped.

### 1. `list_namespaces`
Args: `name_contains?: str`. Returns `[{name, phase, age}]` for allowed namespaces only.

### 2. `list_workloads`
Args: `namespace: str`, `kind?: deployment|statefulset|daemonset|job|cronjob` (default: all except job), `name_contains?: str`.
Returns per workload: `kind, name, desired, ready, updated, available, images[], age, problem?`, where `problem` is the reason/message of the first non-OK condition.

### 3. `get_workload`
Args: `namespace`, `kind: deployment|statefulset|daemonset`, `name`.
Returns: `replicas {desired, ready, updated, available}`, `strategy`, `revision` (`deployment.kubernetes.io/revision`), `containers[] {name, image, requests, limits, env_names[], probes summary}`, `conditions[]`, `current_replicaset`, `previous_replicaset {name, revision, images[]}`, `hpa? {min, max, current, desired}`.
Local mode identifies Deployment ReplicaSets by owner references and revision/template, never by a name prefix. A missing selector yields no matching pods instead of widening to the whole namespace. Job/CronJob references resolve owned Jobs and pods. Kubernetes-native camelCase fields are projected to these output models before redaction.

### 4. `get_pods`
Args: `namespace`, `workload?: {kind, name}`, `label_selector?: str`, `status?: all|not_ready|restarting|pending` (default `all`).
`label_selector` is validated against the Kubernetes selector grammar (`^[A-Za-z0-9._/\-=!, ()]+$` plus a parse check). `workload` is resolved by the spoke to the workload's selector.
Returns per pod: `name, phase, ready "x/y", restarts, last_termination? {reason, exit_code, finished_at}, waiting_reason?, node, age, images[]`.

### 5. `describe_pod`
Args: `namespace`, `name`.
Returns: `phase, node, qos_class, start_time, conditions[]`, `containers[]` and `init_containers[]` with `{name, image, ready, restart_count, state, last_state, requests, limits, env: [{name, value_from?}], probes}` (**no env values**), plus `events[]` (last 20 involving this pod).

### 6. `get_events`
Args: `namespace`, `involved_kind?`, `involved_name?`, `type?: Warning|Normal|all` (default `Warning`), `since_minutes?: int ≤ 180` (default 60).
Returns, newest first: `{last_seen, type, reason, object "Kind/name", count, message}`.

### 7. `get_pod_logs`
Args: `namespace`, `pod`, `container?`, `tail_lines?: int ≤ 200` (default 100), `since_minutes?: int ≤ 60` (default 15), `previous?: bool` (default false; logs of the last terminated container), `contains?: str` (case-insensitive substring filter applied in the spoke; max 100 chars).
Returns `{container, lines[], truncated}`. Each line is capped at 1,000 chars. Redaction applies per line.
Description hint to the model: prefer `previous=true` for CrashLoopBackOff.

### 8. `list_services`
Args: `namespace`. Returns `{name, type, cluster_ip, ports[], selector, ready_endpoints, not_ready_endpoints}`.

### 9. `list_ingresses`
Args: `namespace`. Returns `{name, class, hosts[], rules[] {host, path, service, port}, load_balancer_hostname?, tls_hosts[]}`. Annotations are excluded except `alb.ingress.kubernetes.io/group.name`, `alb.ingress.kubernetes.io/scheme`, and `alb.ingress.kubernetes.io/healthcheck-path`.

### 10. `list_hpas`
Args: `namespace`. Returns `{name, target "Kind/name", min, max, current, desired, metrics[] {name, current, target}, conditions[]}`.

### 11. `get_nodes`
Args: `status?: all|not_ready|pressure` (default `all`), `label_selector?`.
Returns per node: `name, ready, pressure[] (Memory/Disk/PID), instance_type, nodepool, zone, capacity {cpu, memory}, allocatable {cpu, memory}, taints[] (key:effect), age, kubelet_version`.

### 12. `top_pods`
Args: `namespace`, `workload?: {kind, name}`. Returns `{pod, container, cpu_millicores, memory_mib, cpu_request?, memory_request?, memory_limit?}`, sorted by memory descending.

### 13. `list_nodeclaims`
Args: `nodepool?`. Returns `{name, nodepool, instance_type, capacity_type, zone, ready, launched_at, conditions[] (non-True only, with message)}`.

### 14. `list_applications`
Args: `name_contains?`, `sync?: Synced|OutOfSync|Unknown`, `health?: Healthy|Progressing|Degraded|Suspended|Missing|Unknown`.
Returns only Applications whose `spec.destination.namespace` is allowed: `{name, project, destination_namespace, sync, health, revision_short, last_sync {phase, finished_at}}`.

### 15. `get_application`
Args: `name`. `NOT_ALLOWED` if its destination namespace is denied.
Returns: the list fields, plus `conditions[] {type, message}`, `operation_state {phase, message, started_at, finished_at}`, `unhealthy_resources[] {kind, namespace, name, sync, health, message}` (max 50), `history[]` (last 5: `{revision_short, deployed_at}`). No source metadata or other Application spec fields are returned (AGENTS.md invariant 2 and ADR-007).
Local Application queries use `kubernetes.argocd_namespace` (default `argocd`); every unhealthy resource's own namespace is authorized separately. Resources with no namespace are omitted because namespace authorization cannot be established. Short revision IDs use seven characters.

### 16. `get_rollout`
Args: `namespace`, `name`.
Returns: `phase, message, strategy (canary|blueGreen), current_step "i/n", set_weight?, paused {reason?}, aborted, stable_rs {hash, images[]}, canary_rs? {hash, images[]}, replicas {desired, ready, updated, available}, current_analysis_runs[]`.
`current_step` is human one-based `i/n`, clamped to `n/n` when complete. A blueGreen preview ReplicaSet uses the existing `canary_rs` slot; the active ReplicaSet uses `stable_rs`.

### 17. `list_analysis_runs`
Args: `namespace`, `rollout?`, `limit?: int ≤ 10` (default 5).
Returns, newest first: `{name, phase, started_at, message, metrics[] {name, phase, successful, failed, inconclusive, error, last_measurement {value?, message?, finished_at}}}`.
`AnalysisTemplate` provider queries (which can embed URLs and headers) are **not** returned.

### 18. `get_eks_cluster`
Args: none. Returns `{name, version, platform_version, status, endpoint_public_access, endpoint_private_access, addons[] {name, version, status, health_issues[]}}`. No certificate data, no endpoint URL.

### 19. `list_nodegroups`
Args: none. Returns `{name, status, version, release_version, instance_types[], capacity_type, scaling {min, max, desired}, health_issues[] {code, message}}`.

### 20. `get_alb_for_ingress`
Args: `namespace`, `ingress`.
Resolves `status.loadBalancer.ingress[0].hostname`, finds the ALB by DNS name, and returns `{lb_name, scheme, state, target_groups[] {name, port, protocol, health_check_path, targets[] {id, port, state, reason?, description?}}}`. Returns at most 20 target groups and 50 targets per group.

### 21. `get_alb_metrics`
Args: `namespace`, `ingress`, `metrics: [requests|http_5xx_elb|http_5xx_target|http_4xx_target|target_response_time_p95|unhealthy_hosts]` (1–6), `window_minutes?: int ≤ 1440` (default 60).
The spoke picks `period` so that there are ≤ 60 points. CloudWatch namespace `AWS/ApplicationELB` only. Returns `{metric, unit, points[] {t, v}, summary {min, max, avg, last}}`.
Local ALB resolution accepts only an application load balancer matching the ingress DNS name. Count metrics use Sum, latency uses p95, and unhealthy-host metrics use Maximum per target group with fixed LoadBalancer/TargetGroup dimensions and a bounded sum expression. Queries use UTC windows, whole-minute periods, ascending timestamps and bounded MaxDatapoints; missing/partial results are marked incomplete, never treated as a zero-valued healthy series.

## Adding a tool (checklist)

1. Args and output models in `protocol/tools.py`, with a model-facing description.
2. Executor in `spoke/tools/<area>.py`, using only `get`/`list` or AWS `Describe*`/`List*`/`Get*` calls.
3. Row in the summary table above and a definition section.
4. RBAC/IAM additions in [access.md](access.md).
5. Tests: arg validation, denied namespace → `NOT_ALLOWED`, projection drops forbidden fields, redaction, size cap.
6. At least one golden question in [evaluation.md](evaluation.md) that needs the tool.
