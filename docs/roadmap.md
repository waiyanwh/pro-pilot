# Roadmap

The implementation includes a bounded provider-independent hub, fixture/local/
in-cluster spokes, read-only tools, redaction, separate container targets and Helm
charts. A source release does not prove compatibility with every Kubernetes version,
cloud or model. Live acceptance belongs to the operator of each deployment.

## Installation and releases

- Publish versioned images and OCI chart releases.
- Support a generic ingress template alongside the optional AWS ALB path.
- Simplify per-cluster token onboarding without exposing credentials.
- Document Kubernetes/version/provider compatibility from actual acceptance results.

## Deployment acceptance

Verify readiness, wrong-token rejection, denied namespace/write refusals, evidence
quality and bounded latency using dedicated read-only identities. Use synthetic
input before sending cluster data to a newly configured model provider.

## Future decisions

Active-active hub scaling, persistent conversation memory and broader cloud API
support need separately specified designs. They are not included in the current
release. Mutating tools are outside the v1 contract.
