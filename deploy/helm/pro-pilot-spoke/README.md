# pro-pilot-spoke Helm chart

This chart runs one read-only, in-cluster spoke for a single Kubernetes
cluster. It opens no Service or Ingress: the spoke makes an outbound WebSocket
connection to the hub and exposes health probes only on the pod. The chart
enforces one replica and a `Recreate` deployment because connection ownership
is process-local.

## Prerequisites

- A container image built from the repository's `spoke` Dockerfile target and
  pushed to a registry reachable by the cluster. Override `image.repository`
  and `image.tag`; the defaults are local build references.
- A spoke token supplied through an existing Secret (the default), or a
  chart-owned Secret with `hub.tokenSecretRef.create: true` and private values.
- An install namespace (the documented default is `pro-pilot`) and the
  ServiceAccount token projection left enabled.
- If the manually applied `pro-pilot-reader` ClusterRole and
  ClusterRoleBinding already exist, set `rbac.create=false`. The existing
  binding must include the chart ServiceAccount `pro-pilot-spoke` in the
  release namespace. If this chart creates RBAC, it uses the exact read-only
  resources from `spec/access.md`; `rbac.extraGroupBinding` can add the EKS
  access-entry group `pro-pilot:readers`.
- If `aws.enabled=true`, provision the EKS Pod Identity association outside
  this chart and set `aws.region`, `aws.eksClusterName`, and the pinned
  `aws.expectedRoleArnPattern`. Leave it false for non-AWS clusters.

The ConfigMap uses native runtime keys (`cluster_id`, `argocd_namespace`,
`eks_cluster_name`, and snake_case limits). It also records the release
namespace and rendered ServiceAccount name. At startup, the spoke checks the
projected ServiceAccount JWT subject against
`system:serviceaccount:<namespace>:<service_account_name>` before making any
cluster reads. Keep the release namespace and ServiceAccount name aligned with
the existing RBAC binding when `rbac.create=false`.

It sets `mode: in-cluster` and health listening on `0.0.0.0:8080`; readiness
means the spoke has completed the authenticated hub welcome handshake.

For a hub in the same Kubernetes cluster, `hub.url` may use the internal
Service form, for example
`ws://pro-pilot-hub.pro-pilot.svc:8765/spoke/v1/connect` (the optional
`.cluster.local` suffix is also accepted). For a hub in another cluster, use
`wss://` through a private route or an internet-facing ALB. Public plaintext
`ws://` hosts, URL credentials, query strings, and fragments are rejected by
the chart schema. Private routing requires network and DNS reachability to the
hub; an internet-facing hub requires only outbound HTTPS reachability from the
spoke cluster.

Build the image from the repository root with the `spoke` target:

```bash
docker build --target spoke -t REGISTRY/pro-pilot-spoke:0.1.0 .
docker push REGISTRY/pro-pilot-spoke:0.1.0
```

## Chart-managed Secret

Supply the matching raw spoke token in `spoke-secrets.local.yaml` (git-ignored):

```yaml
hub:
  tokenSecretRef:
    create: true
    name: pro-pilot-spoke-token
    key: token
    value: "REPLACE_WITH_RAW_SPOKE_TOKEN"
```

Protect the file with `chmod 600 spoke-secrets.local.yaml`, or decrypt
SOPS-encrypted values locally before passing them to Helm. Add
`-f spoke-secrets.local.yaml` to the manual install/upgrade command below.
An empty token fails rendering. The hub must contain this token's SHA-256 hash
under the same `clusterId`; never supply the hash as the spoke token.

Values stay out of the ConfigMap and Deployment environment literals.
Rendered manifests and Helm release history contain the credentials; base64
encoding is not encryption. Avoid printing them or sending them to CI logs.
See [Helm release storage](https://helm.sh/docs/topics/advanced/#storage-backends).

Keep `hub.tokenSecretRef.create: false` to reuse an existing Secret; enabling
creation does not adopt externally owned Secrets. Uninstalling this release
deletes its chart-owned token Secret. Changing creation from true to false
removes that Secret from the release.

## Manual install command

Review and run this command manually after creating the external token Secret,
or add the private values file above. Chart validation does not install it:

```bash
helm upgrade --install pro-pilot-spoke ./deploy/helm/pro-pilot-spoke \
  --namespace pro-pilot --create-namespace \
  --set image.repository=YOUR_REGISTRY/pro-pilot-spoke \
  --set image.tag=0.1.0 \
  --set clusterId=demo-cluster \
  --set hub.url=wss://pro-pilot.example.com/spoke/v1/connect \
  --set hub.tokenSecretRef.name=pro-pilot-spoke-token \
  --set aws.enabled=false
```

Render and inspect the release before installing:

```bash
helm lint ./deploy/helm/pro-pilot-spoke \
  --set clusterId=demo-cluster \
  --set hub.url=wss://pro-pilot.example.com/spoke/v1/connect
helm template pro-pilot-spoke ./deploy/helm/pro-pilot-spoke \
  --set clusterId=demo-cluster \
  --set hub.url=wss://pro-pilot.example.com/spoke/v1/connect
```

The token is read at pod start. A Helm upgrade with changed chart-managed
values changes the Secret checksum and restarts the spoke. Externally managed
token changes require a manual restart; there is no Secret reloader.
