# pro-pilot contributor guide

Read README.md, then the relevant spec before editing. Specs define behavior;
docs/decisions.md records design decisions. Match existing style and keep changes
limited to the requested task.

## Invariants

- Spokes only use the read APIs/resources in spec/access.md; never add writes,
  exec, attach, port-forward, proxy, impersonation, or secret/configmap reads.
- Enforce schema validation, namespace policy, projection, redaction and limits
  in the spoke before egress. Prompts are not security controls.
- New free-text tools require redaction and scope tests. Tool argument schemas
  come from the Pydantic models in pro_pilot/protocol/tools.py.
- All tool calls and agent turns remain bounded. Slack unfurls stay disabled.
- No tokens, credentials, administrator kubeconfigs or node credentials in
  source, fixtures, logs or production pods.
- In-cluster AWS tools use pinned EKS Pod Identity, never static/profile/node fallbacks.

## Development

Use the commands in README.md. Tests must not contact real Slack, clusters, AWS
or model endpoints; use fixtures and loopback HTTP/WebSocket servers. Only
explicit opt-in live evaluation may use a configured provider.

Repository edits do not authorize deployment or cloud writes. Prepare IaC and
operator instructions; never run apply/destroy, state mutations/imports,
backend migration or force-unlock. Keep live AWS inspection read-only.

Behavior changes update the relevant spec and, when needed, add a superseding
ADR. The agent loop remains framework-free. Logs are structured JSON and must
not contain tool payloads or credentials.
