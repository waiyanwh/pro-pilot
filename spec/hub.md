# Hub Spec

The hub is the only component that talks to Slack and the selected LLM provider. It holds no cluster credentials.

## Configuration

```yaml
# config/hub.local.yaml (no secrets; secrets come from env)
slack:
  # channel ID → cluster IDs that channel may query
  channels:
    C0123456789: [demo-cluster]
  allow_dm: false
  max_question_chars: 1000

llm:
  mode: openai   # fake is offline; bedrock remains opt-in
  base_url: https://llm.example.com/v1
  model_id: your-model-id
  max_output_tokens: 2000
  temperature: 0

agent:
  max_iterations: 8              # model round trips per question
  max_tool_calls: 16             # total tool calls per question
  max_parallel_tool_calls: 4
  turn_deadline_s: 90
  tool_result_chars_to_llm: 12000   # per tool result, after spoke-side caps
  thread_ttl_s: 86400
  thread_max_turns: 20

rate_limits:
  per_user: {requests: 10, per_s: 600}
  global:   {requests: 60, per_s: 600}
  per_user_concurrent: 1

spoke_server:
  listen: 127.0.0.1:8765        # Phase 2: 0.0.0.0:8765 behind ALB TLS
  heartbeat_s: 20
  # cluster_id → token hashes come from env/Secret: PRO_PILOT_SPOKE_TOKENS_JSON

health:
  listen: 127.0.0.1:8080
```

Environment secrets: `PRO_PILOT_SLACK_BOT_TOKEN`, `PRO_PILOT_SLACK_APP_TOKEN`, `PRO_PILOT_SPOKE_TOKENS_JSON` (`{"<cluster_id>": ["sha256:<hex>", ...]}`), and `PRO_PILOT_LLM_API_KEY` for `openai` mode. Only the opt-in Bedrock provider uses the AWS credential chain. Endpoint, model and region have no runtime fallback values; explicitly configure the selected provider.

YAML defaults can be overridden with `PRO_PILOT_<SECTION>__<KEY>` environment variables (for example, `PRO_PILOT_LLM__MODEL_ID` or `PRO_PILOT_AGENT__MAX_TOOL_CALLS=4`). `PRO_PILOT_LLM_MODE` selects `openai`, `fake` or `bedrock`. Runtime settings never load `.env`; the launcher explicitly provides it with `uv run --env-file .env`. Logs are redacted structured JSON on stdout.

## Slack handling

Events subscribed: `app_mention`, `reaction_added` (feedback only).

On `app_mention`:
1. Ignore events from bots, including itself, and `message_changed` subtypes.
2. If the channel is not in `slack.channels`, post an **ephemeral** reply: "pro-pilot isn't enabled in this channel. Ask your platform team to add it." Stop. Do not call the selected model.
3. Strip the bot mention. Reject an empty question or one over `max_question_chars` with a short help message.
4. Apply rate limits. On rejection, reply ephemerally with the retry time.
5. Post a placeholder in the thread (`thread_ts` = event `thread_ts` or `ts`): `:mag: Looking into it…`.
6. Run the agent (below) under `turn_deadline_s`.
7. `chat.update` the placeholder with the answer and a context-block footer:
   `Pro Infra Staging · 👍 Helpful · 👎 Needs improvement`. The answer uses a short
   status headline and optional findings/next actions as defined in [prompts.md](prompts.md).
   Omit the environment when no tool has a cluster; tool names and timing remain in audit records.
8. Every `chat.postMessage` / `chat.update` sets `unfurl_links=false`, `unfurl_media=false`.

DMs (`allow_dm: false`): the `app_mention` event does not fire in DMs, and the `message.im` event is not subscribed. No extra code is needed beyond keeping that subscription off.

Answer length: if the answer exceeds 3,500 characters, truncate at a line boundary and append `There's more detail than I can fit here. Ask about one application for a shorter answer.`

Feedback: on `reaction_added` with `+1` or `-1` on a pro-pilot answer, write a feedback audit record (`trace_id`, user, reaction). No reply.

## Model providers

- `openai`: POST `<llm.base_url>/chat/completions` using `Authorization: Bearer <PRO_PILOT_LLM_API_KEY>`, the configured model, `max_tokens`, temperature, and non-streaming responses. Function schemas come from the same pydantic argument models as the spoke; `tool_choice` is `auto` while tools are enabled. No new SDK/framework dependency is needed: transport uses the existing `aiohttp` library.
- `bedrock`: existing optional Converse transport. Choose an actual Bedrock profile ID and region explicitly; the Gemini gateway ID is not a Bedrock model ID.
- `fake`: explicit offline fixture driver; it never contacts a model API.
- The loop uses one internal conversation envelope (`output.message`, `stopReason`, `usage`). The Chat Completions adapter converts assistant `tool_calls`, tool-result correlation IDs and token usage to/from this envelope; opaque `extra_content` tool metadata is retained for Gemini follow-up calls. Provider failures use `LLMUnavailable`, never a silent fallback to another provider.
- OpenAI-compatible HTTP requests have a 60-second timeout and a 1 MiB response cap, inside the existing 90-second turn deadline. Redirects are not followed, so bearer credentials cannot be forwarded by a redirect. The client uses an explicit user agent because the gateway rejected the default Python user agent during discovery. The HTTP session is reused and closed at hub shutdown.

## Agent loop

```text
history = thread_store.get(channel, thread_ts) or []
system  = render_system_prompt(clusters_for_channel(channel), registry.status())
tools   = tool_names_for(clusters_for_channel(channel))   # adapters export the shared schemas
messages = history + [user(question)]

for i in range(max_iterations):
    resp = llm.converse(system, messages, tool_names=tools, cluster_ids=allowed_clusters)
    messages.append(resp.output.message)
    if resp.stopReason == "end_turn":   return final_text(resp)
    if resp.stopReason == "max_tokens": return final_text(resp) + truncation note
    if resp.stopReason != "tool_use":   return error_answer(resp.stopReason)

    calls = tool_use_blocks(resp)
    enforce(max_tool_calls)                    # excess calls → toolResult status=error "TOOL_BUDGET_EXCEEDED"
    results = await gather_bounded(execute(c) for c in calls, max_parallel_tool_calls)
    messages.append(user(tool_results(results)))
return answer saying the iteration limit was hit + what was found so far (one final converse without tools)
```

`execute(call)`:
1. `call.name == "list_clusters"`: answer locally from the registry.
2. Otherwise read `cluster` from args. If it is not in this channel's clusters, return a toolResult error `CLUSTER_NOT_ALLOWED` without contacting any spoke.
3. If the spoke is not connected, return the error `CLUSTER_UNAVAILABLE`.
4. If the tool is not in that spoke's advertised tools, return the error `TOOL_NOT_SUPPORTED`.
5. Send a `tool_call` with `deadline_ms = min(15000, remaining turn time)`. Await the `tool_result`, or time out with `TIMEOUT`.
6. Trim `data` to `tool_result_chars_to_llm` and add `"truncated": true` if it was cut.
7. Wrap the toolResult content as JSON: `{"cluster": …, "ok": …, "data" | "error": …}`.

Provider errors: Bedrock retries `ThrottlingException` / `ServiceUnavailableException`; the OpenAI-compatible transport retries HTTP 429 / 503. Both retry at most twice with 1 s / 3 s backoff and jitter, inside the turn deadline. Other errors, invalid responses and exhausted retries produce `:warning: The model is unavailable right now. Try again in a minute.` Only safe status/error-type metadata is logged for the gateway; response bodies and credentials are not logged.

Thread store: after a final answer, store `messages`, with each toolResult replaced by its first 2,000 characters, to keep follow-ups cheap. Evict per ADR-011.

Conversation retention counts user-question turns, not tool round trips. Eviction keeps complete question/tool-use/tool-result sequences; an interrupted sequence is replaced by a completed failure reply before it is stored. Audit traces retain completed tool evidence and sum usage across all Converse calls, including a final no-tools call.

## Tool exposure

The hub generates OpenAI function schemas or Bedrock `toolSpec`s from `pro_pilot.protocol.tools.TOOL_REGISTRY`:
- Each spoke tool's input schema gets a required `cluster` property (an `enum` of this channel's cluster IDs) prepended.
- Only tools that **at least one** of the channel's connected clusters advertises are included.
- Add the hub-local tool `list_clusters` (no args). It returns `[{cluster_id, connected, labels, capabilities}]`.

## Spoke server

- WSS endpoint path: `/spoke/v1/connect`. Protocol in [protocol.md](protocol.md).
- Authentication happens at connection time: `Authorization: Bearer <token>` and `X-Pro-Pilot-Cluster: <cluster_id>`. Compare the SHA-256 of the token against the configured hashes for that `cluster_id`, in constant time. On failure, close with code 4401 and log `spoke_auth_failed` with `cluster_id` and peer IP, never the token.
- A new authenticated connection for a `cluster_id` that is already connected replaces the old one. The old connection is closed with 4409, and its pending calls fail with `CLUSTER_UNAVAILABLE`.
- Registry entry: `cluster_id`, `spoke_version`, `protocol_version`, `capabilities`, `tools`, `labels`, `connected_at`, `last_seen`.

## Scaling

Phase 0–2 runs **one replica**. With several replicas, each spoke is connected to only one hub pod, but the Slack event can land on any pod. Before Phase 3 scale-out, pick one of: (a) route tool calls between hub pods (a shared registry plus a forward over an internal service), or (b) have spokes connect to every hub pod (via headless service discovery). This is tracked as roadmap Q6.

The Helm chart enforces one active replica and `Recreate`, including updates. It mounts native YAML in a ConfigMap, references existing Secrets by default or creates them with `secrets.create=true` and private `secrets.<reference>.value` inputs, and keeps the hub ServiceAccount token unmounted. Choose the LLM provider explicitly: an HTTPS OpenAI-compatible endpoint/model, a Bedrock region/model, or fake mode for offline checks. No personal gateway is a deployment default. Bedrock workload identity and registry publication remain separate operator/IaC steps.

The hub listens on `0.0.0.0:8765` and HTTP health on `0.0.0.0:8080` in containers. Optional ALB Ingress requires an explicit scheme and certificate ARN, forwards HTTP/1 WebSocket traffic, and probes `/healthz` on port 8080. SIGTERM/SIGINT trigger transport/client cleanup. Chart-owned credentials are grouped by their configured Secret names and keys; the gateway API key is rendered only for openai mode. A Secret checksum rolls the pod on Helm-managed credential changes. Externally managed Secret changes do not automatically roll the Deployment; token/provider rotation requires a manual restart.

## Health and metrics

- `/healthz`: process alive.
- `/readyz`: Socket Mode connected.
- Log-derived metrics for Phase 2: questions, outcomes, tool errors by code, Bedrock tokens in/out, latency. Prometheus endpoint is deferred until there is a consumer.

## Audit record

One JSON line per question, `event=audit.question`:

```json
{
  "trace_id": "…", "slack_user": "U…", "channel": "C…", "thread_ts": "…",
  "question": "<redacted text>", "clusters": ["demo-cluster"],
  "tool_calls": [{"tool": "get_pods", "cluster": "demo-cluster", "args": {"namespace": "payment"},
                  "ok": true, "error_code": null, "duration_ms": 412, "bytes": 5120, "redactions": 0}],
  "llm": {"model_id": "…", "input_tokens": 0, "output_tokens": 0, "iterations": 3},
  "outcome": "answered|refused|rate_limited|llm_error|deadline", "duration_ms": 14210
}
```

Refused and rate-limited questions are audited without invoking any model API: `clusters` and `tool_calls` are empty and token usage is zero. Successful question records use the actual configured `model_id` and canonical `input_tokens`/`output_tokens` fields; the explicitly offline provider is identified as `fake`.
