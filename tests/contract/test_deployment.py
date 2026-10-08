"""Offline chart contracts: identity wiring, provider selection and read-only RBAC."""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest
import yaml

from pro_pilot.hub.main import load_config as hub_config
from pro_pilot.spoke.main import load_config as spoke_config

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="Helm CLI is not installed")
ROOT = Path(__file__).resolve().parents[2]


def render(component: str, *flags: str) -> list[dict[str, Any]]:
    result = subprocess.run(
        [
            "helm",
            "template",
            f"pro-pilot-{component}",
            str(ROOT / "deploy/helm" / f"pro-pilot-{component}"),
            "--namespace",
            "pro-pilot",
            *flags,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return list(yaml.safe_load_all(result.stdout))


def spoke_flags() -> list[str]:
    return [
        "--set",
        "clusterId=test-cluster",
        "--set",
        "hub.url=ws://pro-pilot-hub.pro-pilot.svc.cluster.local:8765/spoke/v1/connect",
    ]


@pytest.mark.parametrize("component", ["hub", "spoke"])
def test_charts_wire_native_runtime_config_and_external_secrets(
    component: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PRO_PILOT_LLM_MODE", raising=False)
    manifests = render(
        component, *(spoke_flags() if component == "spoke" else ["--set", "config.llm.mode=fake"])
    )
    kinds = {item["kind"] for item in manifests}
    assert "Secret" not in kinds
    if component == "spoke":
        assert "Service" not in kinds and "Ingress" not in kinds
    else:
        service = next(item for item in manifests if item["kind"] == "Service")
        assert service["metadata"]["name"] == "pro-pilot-hub"
    deployment = next(item for item in manifests if item["kind"] == "Deployment")
    assert deployment["metadata"]["name"] == f"pro-pilot-{component}"
    pod = deployment["spec"]["template"]["spec"]
    assert "checksum/secret" not in deployment["spec"]["template"]["metadata"]["annotations"]
    container = pod["containers"][0]
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    assert pod["automountServiceAccountToken"] is (component == "spoke")
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert container["startupProbe"]["httpGet"]["path"] == "/healthz"
    assert container["readinessProbe"]["httpGet"]["path"] == "/readyz"
    assert any(
        env == {"name": "AWS_EC2_METADATA_DISABLED", "value": "true"} for env in container["env"]
    )
    for env in container["env"]:
        if env["name"] != "AWS_EC2_METADATA_DISABLED":
            assert "secretKeyRef" in env["valueFrom"]
    configmap = next(item for item in manifests if item["kind"] == "ConfigMap")
    path = tmp_path / "config.yaml"
    path.write_text(configmap["data"]["config.yaml"])
    if component == "hub":
        assert hub_config(path).spoke_host == "0.0.0.0"
    else:
        cfg = spoke_config(path)
        assert cfg["mode"] == "in-cluster"
        assert cfg["kubernetes"]["namespace"] == "pro-pilot"
        assert cfg["kubernetes"]["service_account_name"] == pod["serviceAccountName"]
        assert cfg["aws"]["enabled"] is False
        role = next(item for item in manifests if item["kind"] == "ClusterRole")
        for rule in role["rules"]:
            assert set(rule["verbs"]) == {"get", "list"}
            assert not set(rule["resources"]) & {
                "secrets",
                "configmaps",
                "pods/exec",
                "serviceaccounts/token",
            }


def test_existing_manual_rbac_can_be_reused() -> None:
    manifests = render("spoke", *spoke_flags(), "--set", "rbac.create=false")
    assert not any(item["kind"] in {"ClusterRole", "ClusterRoleBinding"} for item in manifests)


def test_gateway_and_bedrock_modes_select_only_their_required_secret_refs() -> None:
    for mode, flags in (
        (
            "openai",
            [
                "--set",
                "config.llm.base_url=https://gateway.example.com/v1",
                "--set",
                "config.llm.model_id=test-model",
            ],
        ),
        (
            "bedrock",
            [
                "--set",
                "config.llm.region=ap-southeast-7",
                "--set",
                "config.llm.model_id=approved-inference-profile",
            ],
        ),
    ):
        manifests = render("hub", "--set", f"config.llm.mode={mode}", *flags)
        deployment = next(item for item in manifests if item["kind"] == "Deployment")
        environment = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
        assert any(env["name"] == "PRO_PILOT_LLM_API_KEY" for env in environment) is (mode == "openai")


def test_enabled_ingress_requires_and_emits_explicit_network_and_certificate_choices() -> None:
    flags = [
        "--set",
        "config.llm.mode=fake",
        "--set",
        "ingress.enabled=true",
        "--set",
        "ingress.scheme=internet-facing",
    ]
    with pytest.raises(subprocess.CalledProcessError):
        render("hub", *flags)
    manifests = render(
        "hub",
        *flags,
        "--set",
        "ingress.certificateArn=arn:aws:acm:ap-southeast-7:123456789012:certificate/synthetic-test",
    )
    ingress = next(item for item in manifests if item["kind"] == "Ingress")
    annotations = ingress["metadata"]["annotations"]
    assert annotations["alb.ingress.kubernetes.io/scheme"] == "internet-facing"
    assert annotations["alb.ingress.kubernetes.io/healthcheck-port"] == "8080"
    assert "certificate/synthetic-test" in annotations["alb.ingress.kubernetes.io/certificate-arn"]


@pytest.mark.parametrize(
    ("component", "flags"),
    [
        ("hub", []),
        (
            "hub",
            [
                "--set",
                "config.llm.mode=bedrock",
                "--set",
                "config.llm.region=ap-southeast-7",
                "--set",
                "config.llm.model_id=invalid model id",
            ],
        ),
        ("hub", ["--set", "config.llm.mode=fake", "--set", "replicaCount=2"]),
        (
            "spoke",
            [
                "--set",
                "clusterId=test-cluster",
                "--set",
                "hub.url=ws://public.example.com:8765/spoke/v1/connect",
            ],
        ),
        ("spoke", [*spoke_flags(), "--set", "replicaCount=2"]),
        ("spoke", [*spoke_flags(), "--set", "aws.enabled=true"]),
    ],
)
def test_charts_reject_unsafe_or_incomplete_deployment_settings(
    component: str, flags: list[str]
) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        render(component, *flags)


def managed_secret_values(component: str, mode: str = "fake") -> dict[str, Any]:
    if component == "spoke":
        return {"hub": {"tokenSecretRef": {"create": True, "value": "synthetic-spoke-token"}}}
    llm = {"mode": mode}
    if mode == "openai":
        llm.update(base_url="https://gateway.example.com/v1", model_id="test-model")
    elif mode == "bedrock":
        llm.update(region="ap-southeast-7", model_id="approved-inference-profile")
    return {
        "config": {"llm": llm},
        "secrets": {
            "create": True,
            "slackBotToken": {"value": "synthetic-slack-bot"},
            "slackAppToken": {"value": "synthetic-slack-app"},
            "spokeTokensJson": {
                "value": json.dumps(
                    {"test-cluster": [hashlib.sha256(b"synthetic-spoke-token").hexdigest()]}
                )
            },
            "llmApiKey": {"value": "synthetic-gateway-key"},
        },
    }


def render_values(component: str, values: dict[str, Any], path: Path) -> list[dict[str, Any]]:
    path.write_text(yaml.safe_dump(values))
    return render(component, *(spoke_flags() if component == "spoke" else []), "-f", str(path))


@pytest.mark.parametrize(
    ("component", "mode"), [("hub", "fake"), ("hub", "openai"), ("hub", "bedrock"), ("spoke", "")]
)
def test_managed_secrets_match_refs_and_trigger_rollout(
    component: str, mode: str, tmp_path: Path
) -> None:
    values = managed_secret_values(component, mode)
    # Preserve customized existing name/key settings when chart ownership is enabled.
    ref = (
        values["secrets"]["slackBotToken"]
        if component == "hub"
        else values["hub"]["tokenSecretRef"]
    )
    ref.update(name="custom-credential", key="custom-key")
    manifests = render_values(component, values, tmp_path / "synthetic-values.yaml")
    secrets = {item["metadata"]["name"]: item for item in manifests if item["kind"] == "Secret"}
    assert len(secrets) == (2 if component == "hub" else 1)
    deployment = next(item for item in manifests if item["kind"] == "Deployment")
    environment = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    for env in environment:
        if "valueFrom" in env:
            target = env["valueFrom"]["secretKeyRef"]
            secret = secrets[target["name"]]
            assert secret["type"] == "Opaque"
            decoded = base64.b64decode(secret["data"][target["key"]]).decode()
            assert decoded
            if env["name"] == "PRO_PILOT_SPOKE_TOKENS_JSON":
                assert json.loads(decoded)["test-cluster"] == [
                    hashlib.sha256(b"synthetic-spoke-token").hexdigest()
                ]
    assert any("llm-api-key" in item["data"] for item in secrets.values()) is (
        mode == "openai"
    )
    configmap = next(item for item in manifests if item["kind"] == "ConfigMap")
    assert "synthetic-" not in configmap["data"]["config.yaml"]
    annotations = deployment["spec"]["template"]["metadata"]["annotations"]
    stable = render_values(component, values, tmp_path / "synthetic-values.yaml")
    stable_deployment = next(item for item in stable if item["kind"] == "Deployment")
    assert stable_deployment["spec"]["template"]["metadata"]["annotations"] == annotations
    ref["value"] += "-rotated"
    changed = render_values(component, values, tmp_path / "synthetic-values.yaml")
    changed_deployment = next(item for item in changed if item["kind"] == "Deployment")
    changed_annotations = changed_deployment["spec"]["template"]["metadata"]["annotations"]
    assert changed_annotations["checksum/secret"] != annotations["checksum/secret"]
    assert changed_annotations["checksum/config"] == annotations["checksum/config"]
    if component == "hub":
        values["secrets"]["create"] = False
    else:
        values["hub"]["tokenSecretRef"]["create"] = False
    external = render_values(component, values, tmp_path / "synthetic-values.yaml")
    assert not any(item["kind"] == "Secret" for item in external)


@pytest.mark.parametrize(
    "case", ["empty-hub", "empty-gateway", "invalid-json", "duplicate-key", "empty-spoke"]
)
def test_managed_secrets_reject_missing_or_invalid_values(case: str, tmp_path: Path) -> None:
    component = "spoke" if case == "empty-spoke" else "hub"
    values = managed_secret_values(component, "openai")
    if case == "empty-spoke":
        values["hub"]["tokenSecretRef"]["value"] = ""
    elif case == "empty-hub":
        values["secrets"]["slackBotToken"]["value"] = ""
    elif case == "empty-gateway":
        values["secrets"]["llmApiKey"]["value"] = ""
    elif case == "invalid-json":
        values["secrets"]["spokeTokensJson"]["value"] = "not-json"
    else:
        values["secrets"]["slackAppToken"]["key"] = "slack-bot-token"
    with pytest.raises(subprocess.CalledProcessError):
        render_values(component, values, tmp_path / "synthetic-values.yaml")


@pytest.mark.parametrize("component", ["hub", "spoke"])
def test_chart_packages_include_secret_templates_and_exclude_private_values(
    component: str, tmp_path: Path
) -> None:
    name = f"pro-pilot-{component}"
    chart = tmp_path / name
    shutil.copytree(ROOT / "deploy/helm" / name, chart)
    (chart / f"{component}-secrets.local.yaml").write_text("synthetic-private-values")
    subprocess.run(
        ["helm", "package", str(chart), "--destination", str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    with tarfile.open(next(tmp_path.glob(f"{name}-*.tgz"))) as archive:
        files = archive.getnames()
        assert f"{name}/templates/secret.yaml" in files
        assert not any("secrets.local.yaml" in filename for filename in files)
