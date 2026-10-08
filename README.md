# pro-pilot

A read-only Slack assistant for Kubernetes. Mention the bot in an allowed channel
to ask about workloads, pod failures, logs, deployment versions, nodes, Argo CD
applications, and Argo Rollouts. Answers use evidence from your cluster.

pro-pilot cannot restart, scale, sync, delete, or execute commands in a pod.

## How it works

One **hub** handles Slack and the model provider. A **spoke** runs inside each
cluster and connects outbound to the hub. Each spoke owns namespace policy,
argument validation, redaction and result limits. The hub holds no cluster credentials.

- Kubernetes-only deployment uses standard ServiceAccounts and read-only RBAC.
- Optional AWS tools currently require **Amazon EKS Pod Identity**. Other Kubernetes
  distributions work with `aws.enabled=false`; arbitrary AWS runtimes are not supported.
- Choose an HTTPS OpenAI-compatible Chat Completions endpoint/model or Amazon
  Bedrock Converse. There is no bundled provider endpoint or model default.
- Configuration uses Helm values; credentials use Kubernetes Secrets mapped to
  environment variables. Terraform, Argo CD and SOPS are optional operator choices.

## Install with Helm

Start with [the Helm setup guide](deploy/README.md), configure your Slack app using
[the Slack manifest](spec/slack-app.md), and review [the access contract](spec/access.md).

This repository includes separate hub/spoke Helm charts and Docker targets.
Container images must currently be built and published to your own registry;
there are no published official images or chart releases yet.

For a single cluster, both components can use private Service DNS with no ingress.
Remote spokes require a reachable WSS endpoint. The optional included ingress
is AWS ALB-specific; use your own TLS ingress for another controller.

## Development

```bash
uv sync --all-extras
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest tests/unit tests/contract tests/integration
```

Ordinary tests use synthetic fixtures and loopback servers; they reject external
network connections. Live evaluation is opt-in and requires operator-supplied
read-only credentials and an explicitly selected provider.

Fake-data configuration is in `config/hub.local.yaml` and `config/spoke.local.yaml`.
Both default to fake mode for an offline demonstration; Slack still requires your
own app and tokens if you launch the hub. Runtime settings never load `.env`
automatically; a launcher must explicitly supply it.

## Security and limits

The spoke never reads Kubernetes Secrets or ConfigMaps. It strips pod environment
values and Argo source specifications, redacts free text, and caps every result.
Read [the security model](docs/security.md) before deployment: redaction is not a
promise that every secret format can be detected. Configure namespace scope and
model-provider data handling for your deployment.

One hub replica and one spoke replica per cluster are supported. Updates use
`Recreate`, so a brief disconnect is expected. Multi-cluster connections are
supported through per-cluster tokens and channel-to-cluster mappings; active-active
hub scaling is not supported.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md), [the specs](spec/tools.md), and
[the architecture](docs/architecture.md). Report suspected vulnerabilities
privately using the guidance in [SECURITY.md](SECURITY.md).

## License

Licensed under [MIT](LICENSE).
