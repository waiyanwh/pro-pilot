# Tech Stack

Versions are pinned in `uv.lock` when the project is scaffolded. Pick the latest stable release of each library at that time; do not copy version numbers from older notes.

## Runtime

| Concern | Choice | Notes |
| --- | --- | --- |
| Language | Python 3.13 | One codebase for hub and spoke |
| Concurrency | `asyncio` | Blocking SDKs run via `asyncio.to_thread` behind semaphores |
| Packaging | `uv` + `pyproject.toml` | Extras: `hub`, `spoke`, `dev` |
| Config | `pydantic-settings` + YAML file + env overrides (`PRO_PILOT_` prefix) | Runtime secrets only from env, never runtime YAML; private Helm values may provision Secrets |
| Schemas | `pydantic` v2 | Single source for wire messages and tool args; JSON Schema exported for OpenAI functions and Bedrock `toolSpec` |
| Logging | `structlog` → JSON on stdout | Fields: `ts`, `level`, `event`, `trace_id`, `cluster_id`, `tool`, `duration_ms` |

## Hub (`pro-pilot[hub]`)

| Concern | Library |
| --- | --- |
| Slack | `slack-bolt` (`AsyncApp`, `AsyncSocketModeHandler`) + `slack-sdk`, with `aiohttp` |
| LLM | `aiohttp` Chat Completions adapter for the selected gateway; optional `boto3` Bedrock Converse client |
| Spoke transport (server) | `websockets` (asyncio server). First staging rollout uses private Service WS; remote clusters require WSS/TLS |
| Rate limiting | In-process token buckets (per user, global) |
| Prompt templates | `jinja2` (system prompt, see [prompts.md](prompts.md)) |
| Conversation store | In-process TTL LRU (`cachetools.TTLCache`) |
| Health | Small `aiohttp` server: `/healthz` (process), `/readyz` (Slack Socket Mode connected). No startup model request |

## Spoke (`pro-pilot[spoke]`)

| Concern | Library |
| --- | --- |
| Kubernetes | Official `kubernetes` Python client: in-cluster config (Phase 2+) or kubeconfig (Phase 1, `exec` → `aws eks get-token` with the read-only profile) |
| AWS read APIs | `boto3` (`eks`, `cloudwatch`, `elbv2`). Phase 2 credentials come from EKS Pod Identity through the default chain |
| Transport (client) | `websockets` client with backoff and jitter |
| Redaction | Our own `pro_pilot/spoke/redact.py` (regex + Luhn), no external dependency |

The spoke image does not install `slack-bolt`. The hub image does not install `kubernetes`. This keeps each attack surface small.

## Dev tooling (`pro-pilot[dev]`)

| Tool | Use |
| --- | --- |
| `ruff` | Lint + format |
| `pyright` (strict on `protocol/`, basic elsewhere) | Types |
| `pytest`, `pytest-asyncio` | Tests |
| `moto` / `botocore.stub.Stubber` | Stub AWS calls |
| Kubernetes fixtures | Recorded JSON objects under `tests/fixtures/`, served by the fake spoke and used by unit tests through a fake API client |

## Code layout

```text
pro-pilot/
  pyproject.toml
  uv.lock
  config/
    hub.local.yaml          # Phase 0/1, no secrets
    spoke.local.yaml
  pro_pilot/
    protocol/
      messages.py           # Hello, Welcome, ToolCall, ToolResult, ErrorCode
      tools.py              # one pydantic Args model per tool + TOOL_REGISTRY
      version.py            # PROTOCOL_VERSION = 1
    hub/
      main.py               # entrypoint: pro-pilot-hub
      slack_app.py          # Bolt handlers, posting, formatting
      agent.py              # bounded provider-independent loop
      llm.py                # shared LLMUnavailable / async invocation boundary
      openai.py             # Chat Completions HTTP + conversation translation
      bedrock.py            # optional Converse transport
      registry.py           # connected spokes, capabilities, pending calls
      spoke_server.py       # WSS server, auth, request/response correlation
      policy.py             # channel → clusters, rate limits
      audit.py
    spoke/
      local.py              # guarded executors, capability detection, bounded SDK workers
      incluster.py          # ServiceAccount / pinned Pod Identity bootstrap
      execution.py          # per-call deadlines and upstream truncation metadata
      main.py               # entrypoint: pro-pilot-spoke
      client.py             # WSS client, reconnect, dispatch
      policy.py             # namespace allow/deny, arg validation
      redact.py
      project.py            # field projection helpers (pod, deployment, ...)
      tools/
        k8s.py
        argo.py             # Argo CD Applications
        rollouts.py
        aws.py
      fake.py               # --mode fake: fixture-backed executor
  tests/
    unit/ contract/ integration/ eval/ fixtures/
  deploy/
    helm/pro-pilot-hub/
    helm/pro-pilot-spoke/   # includes ClusterRole pro-pilot-reader
  Dockerfile                # multi-stage; targets: hub, spoke
```

## Container images

- One `Dockerfile` with two final targets, `hub` and `spoke`. Python 3.13 slim Bookworm and the uv builder are pinned by multi-platform digest. Frozen, noneditable installs include only each component's extra; production images contain neither test fixtures nor local configuration/credentials.
- Both targets run as UID/GID 10001. Charts enforce read-only root filesystems, dropped capabilities, seccomp and a writable `/tmp`. No uv, pip or evaluation CLI is included at runtime.
- Images may be published to any registry reachable by the cluster. Install the hub and spoke charts directly with Helm; Terraform, Argo CD, SOPS and a particular AWS account are not application dependencies.

## Why not …

| Alternative | Reason not chosen |
| --- | --- |
| LangChain / LangGraph / Strands | ADR-004: the loop is small; frameworks add churn and hide limits |
| kubectl subprocess as the tool | Unbounded output and argument injection risk; structured client calls are safer |
| A generic Kubernetes MCP server | Exposes the whole API surface; we want a narrow, projected, redacted tool set. MCP can be added later as an *additional* interface over the same spoke tools |
| gRPC | ADR-002 |
| Go | Team decision: Python (ADR-005) |
