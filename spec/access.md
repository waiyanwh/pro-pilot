# Access: Kubernetes RBAC, AWS and model providers

**Audience:** cluster owners deploying pro-pilot. Provision access outside the app;
no code path grants itself additional permissions.

## Kubernetes RBAC

One ClusterRole is used by every spoke, in both modes (ADR-008, ADR-009).

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: pro-pilot-reader
  labels: {app.kubernetes.io/name: pro-pilot, app.kubernetes.io/component: spoke-rbac}
rules:
  - apiGroups: [""]
    resources: [namespaces, nodes, pods, pods/log, events, services, endpoints]
    verbs: [get, list]
  - apiGroups: [events.k8s.io]
    resources: [events]
    verbs: [get, list]
  - apiGroups: [apps]
    resources: [deployments, replicasets, statefulsets, daemonsets]
    verbs: [get, list]
  - apiGroups: [batch]
    resources: [jobs, cronjobs]
    verbs: [get, list]
  - apiGroups: [autoscaling]
    resources: [horizontalpodautoscalers]
    verbs: [get, list]
  - apiGroups: [networking.k8s.io]
    resources: [ingresses]
    verbs: [get, list]
  - apiGroups: [metrics.k8s.io]
    resources: [pods, nodes]
    verbs: [get, list]
  - apiGroups: [argoproj.io]
    resources: [applications, rollouts, analysisruns]
    verbs: [get, list]
  - apiGroups: [karpenter.sh]
    resources: [nodeclaims, nodepools]
    verbs: [get, list]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: pro-pilot-reader
roleRef: {apiGroup: rbac.authorization.k8s.io, kind: ClusterRole, name: pro-pilot-reader}
subjects:
  - kind: Group                       # Phase 1: EKS access entry → this group
    apiGroup: rbac.authorization.k8s.io
    name: pro-pilot:readers
  - kind: ServiceAccount              # Phase 2: in-cluster spoke
    name: pro-pilot-spoke
    namespace: pro-pilot
```

Deliberately **absent**: `secrets`, `configmaps`, `serviceaccounts`, `serviceaccounts/token`, `pods/exec`, `pods/attach`, `pods/portforward`, `pods/proxy`, `nodes/proxy`, `services/proxy`, `analysistemplates`, `clusteranalysistemplates`, `appprojects`, `persistentvolumes`, any `watch` verb, and every mutating verb.

## Helm and ServiceAccounts

The spoke chart creates its dedicated ServiceAccount and get/list-only RBAC by
default. Use `rbac.create=false` only when an existing binding already includes
that ServiceAccount in the release namespace. Namespace allow/deny policy is
additionally enforced in the spoke before egress. Use `rbac.extraGroupBinding`
only when a separate dedicated laptop reader group also needs the same RBAC.

## Optional AWS spoke tools

Kubernetes-only clusters use `aws.enabled=false`. In-cluster AWS tools currently
require EKS Pod Identity: the installed Agent, a dedicated workload role,
`pods.eks.amazonaws.com` trust for AssumeRole/TagSession, and an association for
this cluster/namespace/ServiceAccount. Keep session tags enabled when trust is
constrained with their request tags. No static, profile, node-role or kubeconfig
fallback is allowed in a spoke pod.

Provide `aws.region`, `aws.eksClusterName` and an exact account/role pattern such
as `arn:aws:sts::123456789012:assumed-role/pro-pilot-reader/*`. The spoke checks
STS identity and the EKS cluster ARN/CA. The example account is fictional; use
your actual dedicated role. AWS permission requirements:

- `eks:DescribeCluster`, `eks:ListNodegroups`, `eks:DescribeNodegroup`,
  `eks:ListAddons`, `eks:DescribeAddon`, scoped to the selected cluster and its
  nodegroup/addon ARNs.
- `elasticloadbalancing:DescribeLoadBalancers`, `DescribeTargetGroups`, and
  `DescribeTargetHealth`; these list/describe calls require wildcard resources.
- `cloudwatch:GetMetricData`, which requires wildcard resources.
- `sts:GetCallerIdentity` for identity verification.

No AWS mutations or model invocation permissions belong in the spoke role.
The read-only local laptop mode remains EKS-only and requires a dedicated reader
profile, EKS access entry mapped to a reader group, and guarded kubeconfig exec
settings. See config/spoke.eks.example.yaml and spec/spoke.md.

## Hub model access

OpenAI-compatible mode uses an explicit HTTPS endpoint, model ID and
`PRO_PILOT_LLM_API_KEY`. The endpoint must support native Chat Completions tools.
No model or endpoint is selected automatically.

Bedrock mode requires an explicit region/model or inference profile and an
appropriate workload identity. Non-streaming Converse uses `bedrock:InvokeModel`.
Grant it only on the selected model/profile and destinations required by that
profile. Use a separate hub role; never attach invocation access to the spoke.
Inference-profile routing, entitlement and residency must be reviewed for the
chosen provider/account before real cluster data is sent.

## Verification

With the dedicated deployed identity, permitted get/list operations should work.
Secret/ConfigMap reads, writes, exec, attach, proxy, port-forward and impersonation
must be denied. Hub readiness requires Slack Socket Mode; spoke readiness requires
an authenticated hub welcome. Missing access must fail closed rather than trigger
an administrator credential fallback.
