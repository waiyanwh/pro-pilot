# Architecture

The hub handles Slack Socket Mode, model requests, channel scope, rate limits,
turn budgets and audit metadata. It has no Kubernetes credentials. A spoke inside
each cluster handles tools through its dedicated ServiceAccount and namespace policy.

```text
Slack mention -> hub -> selected model
                   | tool call
                   v
                spoke -> Kubernetes / optional AWS read APIs
                   | projected, redacted, bounded result
                   v
             hub -> threaded Slack answer
```

Spokes connect outbound using an authenticated WebSocket. The hub stores token
hashes indexed by cluster ID; each spoke holds its raw token. Remote connections
use WSS. Same-cluster connections may use private Kubernetes Service DNS over WS.

Tool argument models are the single source for wire validation and model schemas.
Optional metrics, Argo CD, Rollouts and Karpenter tools are advertised only when
available. Missing optional APIs do not grant new access. Kubernetes-only in-cluster
mode does not discover AWS credentials. AWS tools additionally require EKS Pod
Identity and verify the configured role/account and EKS cluster CA/ARN.

Deploy the two charts directly with Helm. The included optional hub ingress uses
AWS ALB; other controllers can expose the hub Service through a separately managed
TLS ingress. No particular cloud account, private registry, GitOps controller or
Terraform layout is required.

Connections, rate limits and thread context are process-local. Charts enforce
one replica and Recreate updates; active-active hub scaling needs a future design.
See [protocol](../spec/protocol.md), [hub](../spec/hub.md), [spoke](../spec/spoke.md)
and [security](security.md) for the contracts.
