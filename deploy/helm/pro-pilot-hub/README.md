# pro-pilot-hub Helm chart

This chart runs one pro-pilot hub replica. The hub owns Slack Socket Mode, the
LLM provider, and authenticated spoke WebSockets. Connection ownership is
process-local, so the chart enforces one replica and a `Recreate` deployment.

## Prerequisites

- A container image built from the repository's `hub` Dockerfile target and
  pushed to a registry reachable by the cluster. Override
  `image.repository` and `image.tag` for the real registry; the defaults are
  local build references.
- Credentials supplied through existing Secret references (the default), or
  chart-owned Secrets with `secrets.create: true` and private values below.
- A Slack Socket Mode app and a provider configuration approved for the data
  sent by this hub. Bedrock mode uses the AWS SDK credential chain and an
  appropriate workload identity configuration.
- An explicit provider selection. The chart intentionally has no provider
  default and never ships a personal gateway or model ID.

The ConfigMap contains only native runtime YAML. It listens on `0.0.0.0:8765`
for spokes and `0.0.0.0:8080` for `/healthz` and `/readyz`.

Build the image from the repository root with the `hub` target:

```bash
docker build --target hub -t REGISTRY/pro-pilot-hub:0.1.0 .
docker push REGISTRY/pro-pilot-hub:0.1.0
```

## Chart-managed Secrets

Set `secrets.create: true` and supply the values in `hub-secrets.local.yaml`
(git-ignored). This example contains placeholders only; fill it privately:

```yaml
secrets:
  create: true
  slackBotToken:
    value: "REPLACE_WITH_SLACK_BOT_TOKEN"
  slackAppToken:
    value: "REPLACE_WITH_SLACK_APP_TOKEN"
  spokeTokensJson:
    value: '{"demo-cluster":["sha256:REPLACE_WITH_64_HEX_DIGIT_TOKEN_HASH"]}'
  llmApiKey:
    value: "REPLACE_WITH_GATEWAY_API_KEY"
```

`spokeTokensJson.value` is a JSON object mapping each cluster ID to its accepted
SHA-256 token hashes. The spoke holds the matching raw token, not the hash.
`llmApiKey.value` is required and rendered only for `openai` mode; omit it
for Bedrock. AWS credentials continue to come from workload identity.

The current `name`/`key` settings remain authoritative. By default all hub
credentials share `pro-pilot-hub-secrets`; different names produce separate
Secrets. Keys within a shared Secret must be unique. Values stay out of the
ConfigMap and Deployment environment literals. Empty required values fail
rendering. `secrets.create: false` preserves existing-Secret references.

Protect the private file with `chmod 600 hub-secrets.local.yaml`, or decrypt
SOPS-encrypted values locally before passing them to Helm. Rendered manifests
and Helm release history contain the supplied credentials; base64 encoding is
not encryption. Avoid printing them or sending them to CI logs. See
[Helm release storage](https://helm.sh/docs/topics/advanced/#storage-backends).

Do not enable creation for a Secret already owned outside this release. Keep
`create: false` to reuse it. Uninstalling this release deletes chart-owned
Secrets, and changing creation from true to false removes them from the release.

## Manual install command

Review and run this command manually after creating external Secrets, or add
`-f hub-secrets.local.yaml` for chart-managed Secrets. It is not executed by
chart validation:

```bash
helm upgrade --install pro-pilot-hub ./deploy/helm/pro-pilot-hub \
  --namespace pro-pilot --create-namespace \
  --set image.repository=REGISTRY/pro-pilot-hub \
  --set image.tag=0.1.0 \
  --set config.llm.mode=openai \
  --set config.llm.base_url=https://your-gateway.example/v1 \
  --set config.llm.model_id=YOUR_OPENAI_COMPATIBLE_MODEL \
  --set 'config.slack.channels.C0123456789[0]=demo-cluster'
```

For Bedrock, select a real Bedrock model or inference-profile ID and region;
do not reuse a gateway or Gemini model ID:

```bash
helm upgrade --install pro-pilot-hub ./deploy/helm/pro-pilot-hub \
  --namespace pro-pilot \
  --set config.llm.mode=bedrock \
  --set config.llm.region=YOUR_AWS_REGION \
  --set config.llm.model_id=YOUR_BEDROCK_MODEL_ID
```

`fake` is available only for explicit offline checks. For example, the chart
lint fixture uses `--set config.llm.mode=fake` and does not contact a provider.

An optional ALB Ingress is disabled by default. Set `ingress.enabled=true`,
provide `ingress.hosts`, set `ingress.scheme` to `internal` or
`internet-facing`, and provide `ingress.certificateArn` for the HTTPS
listener. Use `internal` with private DNS/routing when spokes have private
network reachability; use `internet-facing` when remote spokes need outbound
Internet HTTPS reachability. The supplied ALB defaults route WebSocket traffic
to service port 8765, use an HTTP/1 backend, keep connections idle for 120
seconds, and check pod health on port 8080 at `/healthz`. Target optimizer
settings are left disabled by default for predictable WebSocket routing.

Secret values are read at pod start. Updating chart-managed values through a
Helm upgrade changes the pod's Secret checksum and restarts it. Externally
managed Secret changes require a manual restart; there is no Secret reloader.

Before installing, render and inspect the release:

```bash
helm lint ./deploy/helm/pro-pilot-hub \
  --set config.llm.mode=fake
helm template pro-pilot-hub ./deploy/helm/pro-pilot-hub \
  --set config.llm.mode=fake
```
