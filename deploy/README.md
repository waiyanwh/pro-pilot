# Helm deployment

**Audience:** Kubernetes cluster owners. Helm deploys the application; it does not
provision a cluster, IAM role, registry or model access.

## 1. Prepare Slack and images

Create a Slack Socket Mode app using [the manifest](../spec/slack-app.md), install
it in your workspace, and invite it to the channel where it will answer mentions.
You need a bot token, app-level Socket Mode token and that channel's ID.

Build and push both images to a registry your nodes can pull from:

```bash
docker build --target hub -t YOUR_REGISTRY/pro-pilot-hub:0.1.0 .
docker build --target spoke -t YOUR_REGISTRY/pro-pilot-spoke:0.1.0 .
docker push YOUR_REGISTRY/pro-pilot-hub:0.1.0
docker push YOUR_REGISTRY/pro-pilot-spoke:0.1.0
```

The default chart image names are local build references. For mixed architectures,
use a multi-platform build matching your worker nodes.

## 2. Prepare configuration and private values

Choose a unique cluster ID such as `demo-cluster`. Create `hub-values.yaml`:

```yaml
fullnameOverride: pro-pilot-hub
image:
  repository: YOUR_REGISTRY/pro-pilot-hub
  tag: "0.1.0"
config:
  slack:
    channels:
      C0123456789: [demo-cluster]
  llm:
    mode: openai
    base_url: https://YOUR_ENDPOINT/v1
    model_id: YOUR_TOOL_CAPABLE_MODEL
```

Replace the Slack channel, provider endpoint and model. OpenAI-compatible means an
HTTPS Chat Completions endpoint supporting native tool calls. For Bedrock, set
`mode: bedrock`, `region` and `model_id`, and provision a separate invoke-only hub
workload identity. Omit the API key for Bedrock.

Create `spoke-values.yaml`:

```yaml
fullnameOverride: pro-pilot-spoke
image:
  repository: YOUR_REGISTRY/pro-pilot-spoke
  tag: "0.1.0"
clusterId: demo-cluster
hub:
  url: ws://pro-pilot-hub.pro-pilot.svc.cluster.local:8765/spoke/v1/connect
namespaces:
  allow: [payment]
  deny: ["kube-*", argocd, default, pro-pilot]
rbac:
  create: true
aws:
  enabled: false
```

Replace the allowed namespaces. RBAC gives read APIs from [the access contract](../spec/access.md);
the spoke additionally enforces your namespace policy. Use an existing dedicated
RBAC binding with `rbac.create=false` only if its ServiceAccount/namespace match.

Generate paired private Helm Secret values without printing credentials. Run
this in the repository root, enter your own Slack/model tokens, and keep the
files out of version control. The script refuses to overwrite existing files:

```bash
uv sync --all-extras
uv run python - <<'PYTHON'
import getpass
import hashlib
import json
import os
import secrets
from pathlib import Path

import yaml

paths = [Path("hub-secrets.local.yaml"), Path("spoke-secrets.local.yaml")]
if any(path.exists() for path in paths):
    raise SystemExit("Private values already exist; edit/rotate them explicitly.")
os.umask(0o077)
cluster_id = input("Cluster ID [demo-cluster]: ").strip() or "demo-cluster"
token = secrets.token_urlsafe(32)
entries = {
    "slackBotToken": {"value": getpass.getpass("Slack bot token: ")},
    "slackAppToken": {"value": getpass.getpass("Slack app token: ")},
    "spokeTokensJson": {
        "value": json.dumps({cluster_id: ["sha256:" + hashlib.sha256(token.encode()).hexdigest()]})
    },
}
api_key = getpass.getpass("Model API key (leave empty for Bedrock): ")
if api_key:
    entries["llmApiKey"] = {"value": api_key}
values = [
    {"secrets": {"create": True, **entries}},
    {"hub": {"tokenSecretRef": {"create": True, "value": token}}},
]
for path, value in zip(paths, values, strict=True):
    with path.open("x") as output:
        yaml.safe_dump(value, output)
print("Created private values files. Credentials were not printed.")
PYTHON
```

Existing Kubernetes Secrets are also supported; use the chart's default Secret
references instead of chart-managed values. See the [hub](helm/pro-pilot-hub/README.md)
and [spoke](helm/pro-pilot-spoke/README.md) chart guides.

## 3. Install the hub, then the spoke

Choose your Kubernetes context explicitly. After reviewing values and target:

```bash
helm upgrade --install pro-pilot-hub deploy/helm/pro-pilot-hub \
  --namespace pro-pilot --create-namespace \
  -f hub-values.yaml -f hub-secrets.local.yaml
helm upgrade --install pro-pilot-spoke deploy/helm/pro-pilot-spoke \
  --namespace pro-pilot --create-namespace \
  -f spoke-values.yaml -f spoke-secrets.local.yaml
```

The hub and spoke must use matching cluster IDs, token/hash pair, namespace and
Service DNS name. Avoid two active hubs using the same Slack Socket Mode tokens.

For another cluster, install only its spoke with a distinct cluster ID/token,
add that ID/hash to the hub, update channel scope, and use a reachable WSS URL.
The optional built-in ingress assumes an AWS ALB with an ACM certificate; for
other controllers, leave it disabled and manage a TLS ingress for the hub Service.

## Optional EKS AWS tools

Enable AWS tools only after installing the Pod Identity Agent, creating a dedicated
read-only IAM role and associating it with the spoke's ServiceAccount. Set
`aws.region`, `aws.eksClusterName` and `aws.expectedRoleArnPattern`. This feature
is EKS-only; do not mount static keys, a profile or administrator kubeconfig.
Kubernetes-only clusters need no AWS credentials. Bedrock hub permissions are
separate from the spoke's read-only AWS permissions.

## Acceptance and rollback

Hub `/readyz` reports Slack connectivity; spoke `/readyz` reports its authenticated
hub handshake. Ask a permitted question and verify grounded evidence, then check
that write requests and denied namespaces are refused. Do not infer application
health from bot readiness alone. Missing credentials/permissions fail closed.

Every release has one replica and Recreate updates briefly disconnect it. Use
Helm history/rollback for direct Helm installations. Do not uninstall chart-owned
RBAC or Secrets while they are still required. Existing Secrets are not deleted
by the chart; chart-owned Secrets are stored in Helm release history and removed
on uninstall. Treat rendered Secrets and private files as sensitive.
