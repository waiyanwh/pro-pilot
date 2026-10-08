# Hub ↔ Spoke Protocol (v1)

Transport: WebSocket. Remote Phase 2+ connections run over TLS (`wss://`), terminated at the hub's load balancer. A spoke in the hub's own cluster may connect directly over private Service DNS using `ws://`; its URL is restricted to the internal Service pattern in [spoke.md](spoke.md). Every message is one JSON text frame validated by a pydantic model in `pro_pilot/protocol/messages.py`. Binary frames are rejected.

## Connection

The spoke opens the connection; the hub never connects to a spoke (ADR-002).

```http
GET /spoke/v1/connect HTTP/1.1
Upgrade: websocket
Authorization: Bearer <spoke token>
X-Pro-Pilot-Cluster: demo-cluster
User-Agent: pro-pilot-spoke/<version>
```

| Step | Direction | Message |
| --- | --- | --- |
| 1 | spoke → hub | `hello` (must arrive within 10 s of the upgrade, otherwise the hub closes with 4408) |
| 2 | hub → spoke | `welcome` or close |
| 3+ | hub → spoke | `tool_call` (any number, concurrently) |
| 3+ | spoke → hub | `tool_result` (one per `tool_call`, any order) |

Liveness uses WebSocket ping/pong frames every `heartbeat_s` (default 20 s), sent by both sides. No pong within 2 × `heartbeat_s` means the side closes the connection.

## Messages

All messages share `{"type": "<name>", "v": 1, ...}`.

### `hello` (spoke → hub)

```json
{
  "type": "hello", "v": 1,
  "cluster_id": "demo-cluster",
  "spoke_version": "0.1.0",
  "labels": {"project": "demo", "env": "staging", "cloud": "aws", "region": "ap-southeast-7"},
  "capabilities": ["k8s", "metrics", "argocd", "rollouts", "karpenter", "aws"],
  "tools": ["list_namespaces", "get_pods", "describe_pod", "..."],
  "limits": {"max_result_bytes": 32768, "tool_timeout_s": 15}
}
```

The hub rejects the connection (close 4400) if `cluster_id` differs from the `X-Pro-Pilot-Cluster` header. Tool names missing from the hub's `TOOL_REGISTRY` are ignored with a `spoke_unknown_tool` warning; the hub only exposes tools it knows.

### `welcome` (hub → spoke)

```json
{"type": "welcome", "v": 1, "session_id": "uuid", "heartbeat_s": 20}
```

### `tool_call` (hub → spoke)

```json
{
  "type": "tool_call", "v": 1,
  "id": "uuid",
  "trace_id": "uuid",
  "tool": "get_pod_logs",
  "args": {"namespace": "payment", "pod": "payment-api-7d9c-abcde", "tail_lines": 100},
  "deadline_ms": 15000
}
```

`args` never contains `cluster`; the hub strips it after routing.

### `tool_result` (spoke → hub)

Success:

```json
{
  "type": "tool_result", "v": 1, "id": "uuid", "ok": true,
  "data": { "...": "tool-specific output model" },
  "truncated": false, "redactions": 2, "duration_ms": 381
}
```

Failure:

```json
{
  "type": "tool_result", "v": 1, "id": "uuid", "ok": false,
  "error": {"code": "NOT_ALLOWED", "message": "namespace 'kube-system' is not allowed on this cluster"},
  "duration_ms": 3
}
```

## Error codes

| Code | Raised by | Meaning |
| --- | --- | --- |
| `UNKNOWN_TOOL` | spoke | Tool not advertised by this spoke |
| `INVALID_ARGS` | spoke | Args failed schema validation |
| `NOT_ALLOWED` | spoke | Namespace (or resource) denied by spoke policy |
| `FORBIDDEN` | spoke | Kubernetes/AWS returned 403 (RBAC/IAM gap) |
| `NOT_FOUND` | spoke | Object does not exist |
| `TIMEOUT` | spoke or hub | Deadline exceeded |
| `UPSTREAM_ERROR` | spoke | Other API error (message is one line, redacted) |
| `RATE_LIMITED` | spoke | `max_concurrent_tools` queue is full for longer than 2 s |
| `CLUSTER_NOT_ALLOWED` | hub | Cluster not mapped to the Slack channel |
| `CLUSTER_UNAVAILABLE` | hub | Spoke not connected or disconnected mid-call |
| `TOOL_NOT_SUPPORTED` | hub | Connected spoke lacks the tool's capability |
| `TOOL_BUDGET_EXCEEDED` | hub | Per-question tool-call budget used up |

The LLM sees these codes, and the system prompt tells it how to explain each one to users (see [prompts.md](prompts.md)).

## Close codes

| Code | Sender | Meaning | Spoke reaction |
| --- | --- | --- | --- |
| 1000 | either | Normal shutdown | Reconnect with backoff |
| 4400 | hub | Bad hello / mismatch | Reconnect with backoff; log error |
| 4401 | hub | Authentication failed | Retry every 300 s; log error |
| 4408 | hub | No hello in time | Reconnect with backoff |
| 4409 | hub | Replaced by a newer connection for the same cluster | Do not reconnect immediately; wait 30 s |
| 4426 | hub | Protocol version unsupported | Exit non-zero |

## Limits

- Max frame size: 1 MiB (both sides configure `max_size`). Spoke results are already capped at `max_result_bytes`.
- Max in-flight `tool_call`s per spoke connection: 16. The hub queues excess calls.
- Unknown `id` in a `tool_result` is logged and dropped. A late result after a hub timeout is also dropped.

## Versioning

- `v` is the protocol major version. The hub supports `[current, current-1]`. A breaking change bumps `v`. A new optional field does not.
- Adding a tool is not breaking: a spoke may advertise a tool before the hub knows it (ignored with a warning). Roll out the hub first so the new tool is usable as soon as spokes advertise it.
