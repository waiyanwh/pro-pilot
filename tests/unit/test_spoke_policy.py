from pro_pilot.protocol.tools import TOOL_REGISTRY
from pro_pilot.spoke.policy import NamespacePolicy
from pro_pilot.spoke.project import project_output


def test_deny_wins_over_allow_and_glob_edges_are_explicit() -> None:
    policy = NamespacePolicy(("*",), ("kube-*", "kubecost"))
    assert policy.allowed("payment")
    assert not policy.allowed("kube-system")
    assert not policy.allowed("kubecost")
    assert policy.allowed("kubernetes")


def test_cluster_wide_lists_filter_denied_objects_without_counts() -> None:
    policy = NamespacePolicy(("*",), ("kube-*", "argocd"))
    namespaces = [
        {"name": "payment", "phase": "Active"},
        {"name": "kube-system", "phase": "Active"},
    ]
    applications = [
        {"name": "payment", "destination_namespace": "payment"},
        {"name": "platform", "destination_namespace": "argocd"},
    ]
    assert policy.filter_items("list_namespaces", namespaces) == [namespaces[0]]
    assert policy.filter_items("list_applications", applications) == [applications[0]]


def test_application_with_missing_destination_namespace_is_excluded() -> None:
    """Regression: an Application CR with no `destination_namespace` at all
    (malformed or still reconciling) must be treated as out-of-scope and
    dropped, not treated as "no namespace restriction" and shown. The old
    filter used `not isinstance(..., str) or allowed(...)`, which let a
    missing/non-string `destination_namespace` through unconditionally."""

    policy = NamespacePolicy(("payment",), ("kube-*",))
    applications = [
        {"name": "allowed", "destination_namespace": "payment"},
        {"name": "denied", "destination_namespace": "kube-system"},
        {"name": "unscoped"},
    ]
    assert policy.filter_items("list_applications", applications) == [applications[0]]


def test_projection_drops_env_values_and_raw_object_fields() -> None:
    raw = {
        "phase": "Running",
        "containers": [
            {
                "name": "api",
                "env": [{"name": "PASSWORD", "value": "hunter2"}],
            }
        ],
        "metadata": {
            "managedFields": [{"manager": "kubectl"}],
            "annotations": {"kubectl.kubernetes.io/last-applied-configuration": "secret"},
        },
    }
    projected = project_output(TOOL_REGISTRY["describe_pod"].output_model, raw)
    assert "hunter2" not in str(projected)
    assert "managedFields" not in str(projected)
    assert "last-applied-configuration" not in str(projected)


def test_application_output_is_status_only_and_drops_spec_source() -> None:
    """ADR-007 / AGENTS.md invariant 2: Argo CD Applications are status-only.
    `spec.source` (which can carry inline Helm values) is never modeled, so
    even a raw CR with embedded secrets in `source.helm.values*` can never
    reach the output, regardless of what the raw object contains."""

    output_model = TOOL_REGISTRY["get_application"].output_model
    assert "source" not in output_model.model_fields

    raw = {
        "name": "identity",
        "destination_namespace": "identity",
        "source": {
            "repo_url": "https://github.com/example/identity",
            "path": "deploy/identity",
            "helm": {"values": "password=hunter2", "valuesObject": {"password": "hunter2"}},
        },
    }
    projected = project_output(output_model, raw)
    assert "source" not in projected
    assert "hunter2" not in str(projected)
    assert "valuesObject" not in str(projected)


def test_container_probes_are_narrowed_and_drop_unlisted_fields() -> None:
    """Regression: `probes` used to be an untyped `dict[str, Any]` pass-through.
    `strip_forbidden` has a fixed, key-name-based deny-list (e.g.
    `http_headers`, `command`); it cannot anticipate every key a probe
    object might carry (e.g. a `basic_auth` block some custom webhook probe
    adds). Only a narrow output model whose extra fields are ignored (not a
    generic deny-list keyed by name) can guarantee that *only* the catalog
    fields in spec/tools.md #3/#5 ("probes summary") ever cross the
    boundary, regardless of what else a raw probe spec carries.
    """

    raw = {
        "phase": "Running",
        "containers": [
            {
                "name": "api",
                "probes": {
                    "liveness": {
                        "type": "http",
                        "path": "/healthz",
                        "port": "8080",
                        "period_seconds": 10,
                        "basic_auth": {"password": "hunter2"},
                    }
                },
            }
        ],
    }
    projected = project_output(TOOL_REGISTRY["describe_pod"].output_model, raw)
    dumped = str(projected)
    assert "hunter2" not in dumped
    assert "basic_auth" not in dumped
    assert projected["containers"][0]["probes"]["liveness"]["path"] == "/healthz"


def test_application_operation_state_and_history_drop_unlisted_fields() -> None:
    """Regression: `operation_state`, `unhealthy_resources`, and `history` used
    to be untyped `dict`/`list[dict]` pass-throughs. A raw Argo operation
    result or resource can embed arbitrary extra data (e.g. `sync_result`,
    `live_state`) under keys that are not in `strip_forbidden`'s fixed
    deny-list, so only a narrow output model - not the generic projection
    pass - can keep it out. The output model must keep only spec/tools.md's
    catalog fields for each.
    """

    raw = {
        "name": "identity",
        "destination_namespace": "identity",
        "operation_state": {
            "phase": "Failed",
            "message": "sync failed",
            "started_at": "2026-10-05T07:59:00Z",
            "finished_at": "2026-10-05T08:00:00Z",
            "sync_result": {"resources": [{"api_key": "hunter2"}]},
        },
        "unhealthy_resources": [
            {
                "kind": "Deployment",
                "namespace": "identity",
                "name": "identity",
                "sync": "OutOfSync",
                "health": "Degraded",
                "message": "deployment unavailable",
                "live_state": {"client_secret": "hunter2"},
            }
        ],
        "history": [
            {
                "revision_short": "abc1234",
                "deployed_at": "2026-10-05T08:00:00Z",
                "deployed_by": {"token": "hunter2"},
            }
        ],
    }
    projected = project_output(TOOL_REGISTRY["get_application"].output_model, raw)
    dumped = str(projected)
    assert "hunter2" not in dumped
    assert "sync_result" not in dumped
    assert "live_state" not in dumped
    assert "deployed_by" not in dumped
    assert projected["operation_state"]["phase"] == "Failed"
    assert projected["unhealthy_resources"][0]["kind"] == "Deployment"
    assert projected["history"][0]["revision_short"] == "abc1234"
