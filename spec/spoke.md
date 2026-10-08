# Spoke Spec

The spoke runs read-only tools against one cluster and is the **enforcement point** for data policy (ADR-001). It never trusts arguments from the hub.

## Modes

| Mode | Phase | Kubernetes auth | AWS auth | Data |
| --- | --- | --- | --- | --- |
| `fake` | 0 | none | none | Fixtures in `tests/fixtures/clusters/<cluster_id>/` |
| `local` | 1 | kubeconfig context whose `exec` runs `aws eks get-token --cluster-name <name> --region ap-southeast-7 --profile pro-pilot-staging-ro` | Same AWS profile (`pro-pilot-staging-ro` assumes the spoke read-only role) | Real cluster |
| `in-cluster` | 2+ | Projected ServiceAccount, bound to ClusterRole `pro-pilot-reader` | Pinned EKS Pod Identity when enabled; no AWS session otherwise | Real cluster |

On startup in `local` mode, the spoke calls `sts:GetCallerIdentity` through the explicitly configured profile and refuses to continue unless the ARN matches `aws.expected_role_arn_pattern`. The pattern must pin the AWS account and role, allowing a wildcard only for the STS session suffix. The same check runs before reconnect capability discovery. EKS `DescribeCluster` binds the chosen kubeconfig endpoint to the configured cluster; kubeconfig must use only `aws eks get-token` for the same region, cluster and guarded profile. Static credentials, alternate role arguments, insecure TLS and credential environment overrides are rejected. Generated kubeconfig `AWS_PROFILE` entries are converted to an explicit `--profile` in memory, never persisted, so inherited environment keys cannot bypass the guard.

In-cluster mode uses only `load_incluster_config`, never kubeconfig discovery. `kubernetes.namespace` and `kubernetes.service_account_name` must match the projected JWT subject. This subject check does not verify its signature; Kubernetes authenticates the token and enforces RBAC on the first read. HTTPS and the mounted cluster CA are required. When `aws.enabled=false`, no AWS SDK session or credential discovery occurs. Local mode remains EKS-only even when AWS tool advertisement is disabled.

When AWS tools are enabled in-cluster, region, EKS name and the pinned role pattern are required. The SDK must use the EKS Pod Identity container provider; profiles, static credentials and node-role credentials are rejected. STS is checked before cluster credentials are loaded. The configured EKS ARN and CA must match the mounted ServiceAccount cluster; the client then uses the verified EKS endpoint with token refresh retained. STS is rechecked before reconnect discovery. Pod Identity trust and associations belong in IaC, not Helm.

## Configuration

```yaml
# config/spoke.local.yaml
cluster_id: demo-cluster          # stable, unique, [a-z0-9-]{3,40}
labels: {project: demo, env: staging, cloud: aws, region: ap-southeast-7}
mode: local

hub:
  url: ws://127.0.0.1:8765/spoke/v1/connect    # Phase 2: wss://<hub host>/spoke/v1/connect
  # token from env PRO_PILOT_SPOKE_TOKEN

kubernetes:
  kubeconfig: ~/.kube/config
  context: pro-pilot-staging-ro

aws:
  enabled: true
  region: ap-southeast-7
  profile: pro-pilot-staging-ro                # local mode only
  expected_role_arn_pattern: "arn:aws:sts::123456789012:assumed-role/pro-pilot-reader/*"
  eks_cluster_name: example-cluster     # verify (roadmap Q5)

namespaces:
  allow: ["*"]                                  # globs
  deny:  ["kube-*", "argocd", "argo-rollouts", "karpenter", "datadog*", "hoop*",
          "cert-manager", "external-secrets", "kubecost", "default"]

limits:
  tool_timeout_s: 15            # hard cap; the hub deadline can only shorten it
  max_result_bytes: 32768       # after projection + redaction, before send
  max_list_items: 200
  max_log_lines: 200
  max_log_since_s: 3600
  max_concurrent_tools: 4
```

Choose your own dedicated reader role and pin its STS account/role ARN in configuration. Never use an administrator role for a spoke.

## Capability detection (startup)

| Capability | Advertised when |
| --- | --- |
| `k8s` | `list namespaces` succeeds |
| `metrics` | `metrics.k8s.io` advertises `pods` in a served API version |
| `argocd` | `applications.argoproj.io` is discoverable and a capped cross-namespace Application list succeeds |
| `rollouts` | Both `rollouts.argoproj.io` and `analysisruns.argoproj.io` are discoverable |
| `karpenter` | Both `nodepools.karpenter.sh` and `nodeclaims.karpenter.sh` are discoverable |
| `aws` | `aws.enabled` and `eks:DescribeCluster` on the configured cluster succeeds |

The tools advertised in `hello` are exactly the tools whose capability is present (mapping in [tools.md](tools.md)). Capabilities are re-detected on every reconnect.

Discovery chooses the served preferred version (with the documented v1/v1beta1/v1alpha1 preference when available), advertises no unavailable optional tools, and fails startup if the core namespace read fails. Local SDK work runs in a fixed-size worker pool; cancelled requests do not release their actual worker slot before the SDK call ends. SDK network timeouts, per-call deadlines, one-page item limits and an 8 MiB raw Kubernetes response cap bound retrieval before safe projection. A continuation token or any omitted bounded data propagates `truncated=true` rather than claiming a complete cluster-wide view.

## Tool execution pipeline

Every `tool_call` goes through these steps in this order. A step that fails returns a `tool_result` with the error code shown.

1. **Lookup.** Is the tool known and advertised? Otherwise `UNKNOWN_TOOL`.
2. **Validate.** Parse args into the tool's pydantic model, with `extra="forbid"`. Otherwise `INVALID_ARGS`.
3. **Policy.** Every namespace the call would touch must pass `allowed(ns)`: match any `allow` glob and no `deny` glob. Otherwise `NOT_ALLOWED`. For "all namespaces" list calls, the spoke lists cluster-wide and **filters out** denied namespaces before projection. Never return a denied object, even as a count. The Argo Application tools are the documented exception: they read `applications.argoproj.io` in the Argo CD namespace but return only Applications whose `spec.destination.namespace` passes `allowed()`.
4. **Execute.** Run the read-only client call(s) with `timeout = min(tool_timeout_s, deadline_ms)`, `limit=max_list_items` on list calls (no continue-token paging beyond one page), and `asyncio.Semaphore(max_concurrent_tools)`. API 403 → `FORBIDDEN`, 404 → `NOT_FOUND`, timeout → `TIMEOUT`, anything else → `UPSTREAM_ERROR` with a one-line message.
5. **Project.** Convert API objects to the small output models in [tools.md](tools.md). Never pass through raw objects. Strip `env[].value`, `metadata.managedFields`, the `kubectl.kubernetes.io/last-applied-configuration` annotation, and any Argo `spec.source(s).helm.values*`.
6. **Redact.** Run `redact()` on every string ([security.md § Redaction](../docs/security.md#redaction-spoke-side)) and count the matches.
7. **Cap.** Serialize; if over `max_result_bytes`, drop list items from the end (logs: keep the **last** lines), then shorten oversized string fields if necessary. Set `truncated=true` whenever a list, log-line, or byte cap omits data. If the object cannot fit even after reduction, return `UPSTREAM_ERROR`, never an oversized success. Redaction always precedes string shortening, including the 1,000-character log-line cap.
8. **Respond.** Send `tool_result` with `ok`, `data`, `truncated`, `redactions`, `duration_ms`.

## Connection behaviour

- In-cluster connections require `wss://…/spoke/v1/connect` for remote endpoints. The same-cluster topology may use private `ws://<service>.<namespace>.svc[.cluster.local]:<port>/spoke/v1/connect`; other plaintext hosts, URL credentials, queries and fragments are rejected. The internal exception assumes a trusted Kubernetes Service and private cluster networking.
- Connect, send `hello`, then wait for `welcome` (protocol in [protocol.md](protocol.md)).
- Reconnect on any close or error, with exponential backoff `min(60, 2^n)` seconds and ±20% jitter. Reset after 60 s of stable connection.
- Close code `4401` (auth): keep retrying, but at a fixed 300 s interval, logging `spoke_auth_rejected` at error level each time.
- Close code `4426` (protocol version unsupported): log and exit non-zero, so the deployment shows CrashLoopBackOff and gets noticed.

## Logging

One JSON line per tool call: `cluster_id`, `trace_id`, `tool`, `args` (validated), `ok`, `error_code`, `duration_ms`, `bytes`, `redactions`, `truncated`. Never log result data.

Phase 0 uses fixture responses only; executor-side selectors and API time windows are not live Kubernetes query semantics. The full validation/policy/projection/redaction/cap pipeline still applies to every fixture response.

Phase 1 local mode implements all executor-side filters, owned workload resolution, log tail/time/previous-container arguments, Kubernetes quantities and fixed AWS metric presets. Kubernetes calls use the official `ApiClient` with a literal GET method and a fixed resource allowlist; no kubectl subprocess or arbitrary API path is exposed. HTTP 403/404/timeouts become generic FORBIDDEN/NOT_FOUND/TIMEOUT results. The startup guard uses STS GetCallerIdentity; other AWS executors use only the catalog's Describe/List/GetMetricData calls.

## Helm chart `pro-pilot-spoke` (Phase 2+)

| Value | Default | Purpose |
| --- | --- | --- |
| `clusterId` | required | |
| `labels` | `{}` | Advertised labels |
| `hub.url` | required | WSS for remote hubs, or the private same-cluster Service URL above |
| `hub.tokenSecretRef` | required | Secret name/key holding the spoke token; `create=false` reuses an existing Secret, `create=true` requires private `.value` input and creates it with Helm |
| `namespaces.allow` / `.deny` | as above | Policy |
| `rbac.create` | `true` | ClusterRole `pro-pilot-reader` + ClusterRoleBinding to the ServiceAccount |
| `rbac.extraGroupBinding` | `""` | Optional extra ClusterRoleBinding subject group (e.g. `pro-pilot:readers` for the EKS access entry) |
| `aws.enabled` | `false` | Turns on AWS tools; requires a Pod Identity association managed in IaC |
| `aws.region` / `.eksClusterName` / `.expectedRoleArnPattern` | empty | Required when AWS tools are enabled |
| `resources` | requests `50m/128Mi`, limits `256Mi` | |
| `securityContext` | non-root, read-only root FS, drop ALL capabilities, seccomp `RuntimeDefault` | |

Network: egress HTTPS/443 to the hub host and to the Kubernetes API (plus AWS APIs if `aws.enabled`). No Service, no Ingress.

The chart sets the native namespace and ServiceAccount name from the release, enforces one replica with `Recreate`, mounts a nonsensitive ConfigMap, references an existing token Secret by default or creates it with `hub.tokenSecretRef.create=true` and disables node IMDS discovery. Use `rbac.create=false` with the already-applied `pro-pilot-reader` binding and the exact matching namespace/ServiceAccount; Helm does not adopt manual resources automatically.

HTTP `/healthz` reports process liveness and `/readyz` returns 200 only after authenticated `welcome`, otherwise 503 `hub_disconnected`. A disconnected hub must not fail liveness. In-cluster mode defaults to `0.0.0.0:8080`; local/fake mode adds health only when explicitly configured. SIGTERM/SIGINT close health, the WebSocket and SDK workers. Secret-based environment credentials are loaded at startup; Helm-managed token changes roll the pod through a Secret checksum, while externally managed rotation requires a manual workload restart.
