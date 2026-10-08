# Design decisions

These decisions describe the public implementation, not a particular deployment.

## ADR-001: Hub and spokes

**Status:** Accepted for the source release.

**Decision.** One hub routes questions; one spoke per cluster owns cluster credentials, policy and tools. The hub has no Kubernetes credentials.

## ADR-002: Outbound authenticated WebSockets

**Status:** Accepted for the source release.

**Decision.** Spokes dial out to the hub. Remote traffic requires WSS; private same-cluster Service URLs may use WS. Tokens are scoped to cluster IDs.

## ADR-003: Explicit model selection

**Status:** Accepted for the source release.

**Decision.** Operators choose an OpenAI-compatible tool-capable endpoint/model or Bedrock Converse model/profile. Model data handling and regions are deployment-specific.

## ADR-004: Framework-free bounded agent loop

**Status:** Accepted for the source release.

**Decision.** The agent loop uses provider adapters and shared Pydantic-generated tool schemas. Iteration, tool-call and total-time limits stay explicit.

## ADR-005: Python and typed wire contracts

**Status:** Accepted for the source release.

**Decision.** Python 3.13+, asyncio, Pydantic, official Kubernetes/AWS clients and native WebSockets. Blocking SDK work uses bounded workers.

## ADR-006: Read-only forever in v1

**Status:** Accepted for the source release.

**Decision.** There are no restart, scale, sync, rollout promotion, delete or exec tools. Read-only RBAC/IAM and literal read paths enforce the boundary.

## ADR-007: Argo state through Kubernetes CRs

**Status:** Accepted for the source release.

**Decision.** Read Application, Rollout and AnalysisRun status through Kubernetes without an Argo API token. Source/spec values are omitted.

## ADR-008: Channel scope and namespace policy

**Status:** Accepted for the source release.

**Decision.** Hub channels map to cluster IDs. Every spoke enforces namespace allow/deny, with deny winning. DM access is disabled.

## ADR-009: Dedicated reader identity

**Status:** Accepted for the source release.

**Decision.** Local EKS reads and in-cluster Kubernetes reads use dedicated read-only identities; administrator credentials never run a spoke.

## ADR-010: Portable deployment ownership

**Status:** Accepted for the source release.

**Decision.** Operators decide hub placement and supply their own registry, Secrets, model credentials and network routing. No organization Terraform or GitOps repository is required.

## ADR-011: Single active process

**Status:** Accepted for the source release.

**Decision.** One hub replica and one spoke per cluster use Recreate. Shared ownership/forwarding would be needed before active-active hub scaling.

## ADR-012: Configuration without implicit dotenv

**Status:** Accepted for the source release.

**Decision.** YAML plus explicit environment overrides configure runtime. Secrets come from the launcher or Kubernetes Secrets; runtime never loads .env automatically.

## ADR-013: Provider-independent tool loop

**Status:** Accepted for the source release.

**Decision.** OpenAI-compatible Chat Completions and Bedrock Converse share the bounded conversation/tool envelope. There is no automatic provider fallback.

## ADR-014: Guarded readers and offline fixtures

**Status:** Accepted for the source release.

**Decision.** Fixtures and loopback servers cover executor transformations, protocol, scope and redaction. Offline checks do not prove live IAM/RBAC or model quality.

