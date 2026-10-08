from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from kubernetes.config import incluster_config

from pro_pilot.spoke import incluster
from pro_pilot.spoke.local import IdentityRejected, LocalExecutor
from pro_pilot.spoke.main import build_client
from pro_pilot.spoke.policy import NamespacePolicy
from tests.local_support import ROLE_PATTERN, FixtureApi, FixtureAWS, load_fixture

CA = b"synthetic-cluster-ca\n"
ENDPOINT = "https://eks.example.invalid"


def token(subject: str = "system:serviceaccount:pro-pilot:pro-pilot-spoke") -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"sub": subject}).encode()).decode().rstrip("=")
    return f"header.{payload}.synthetic-signature"


@pytest.fixture
def bootstrap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    for name in (*incluster._LOCAL_CREDENTIAL_ENV, "KUBECONFIG"):
        monkeypatch.delenv(name, raising=False)
    ca = tmp_path / "ca.crt"
    ca.write_bytes(CA)
    state: dict[str, Any] = {
        "loaded": 0,
        "api_created": 0,
        "sessions": 0,
        "subject": "system:serviceaccount:pro-pilot:pro-pilot-spoke",
        "verify_ssl": True,
        "method": "container-role",
    }
    api = FixtureApi(load_fixture("k8s")["routes"])
    aws = FixtureAWS(
        {
            "describe_cluster": {
                "cluster": {
                    "arn": "arn:aws:eks:test-region:123456789012:cluster/test-cluster",
                    "endpoint": ENDPOINT,
                    "certificateAuthority": {"data": base64.b64encode(CA).decode()},
                }
            }
        }
    )

    def load(*, client_configuration: Any) -> None:
        state["loaded"] += 1
        client_configuration.host = "https://10.0.0.1:443"
        client_configuration.ssl_ca_cert = str(ca)
        client_configuration.verify_ssl = state["verify_ssl"]
        client_configuration.api_key["BearerToken"] = "Bearer " + token(state["subject"])

    def api_client(configuration: Any) -> FixtureApi:
        state["api_created"] += 1
        state["configuration"] = configuration
        return api

    class Session:
        def __init__(self, *, region_name: str) -> None:
            assert region_name == "test-region"
            state["sessions"] += 1

        def get_credentials(self) -> Any:
            return SimpleNamespace(method=state["method"])

        def client(self, name: str, **kwargs: Any) -> Any:
            return aws

    monkeypatch.setattr(incluster.config, "load_incluster_config", load)
    monkeypatch.setattr(incluster.client, "ApiClient", api_client)
    monkeypatch.setattr(incluster.boto3, "Session", Session)
    state.update(api=api, aws=aws, ca=ca)
    return state


def settings(*, aws: bool = False) -> dict[str, Any]:
    return {
        "cluster_id": "test-cluster",
        "mode": "in-cluster",
        "hub": {"url": "ws://hub.pro-pilot.svc.cluster.local:8765/spoke/v1/connect"},
        "kubernetes": {"namespace": "pro-pilot", "service_account_name": "pro-pilot-spoke"},
        "aws": {
            "enabled": aws,
            "region": "test-region",
            "eks_cluster_name": "test-cluster",
            "expected_role_arn_pattern": ROLE_PATTERN,
        },
        "namespaces": {"allow": ["payment", "payments"], "deny": ["kube-*"]},
    }


def test_subject_check_uses_real_kubernetes_service_account_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_path = tmp_path / "token"
    token_path.write_text(token())
    cert_path = tmp_path / "ca.crt"
    cert_path.write_bytes(CA)
    monkeypatch.setattr(incluster_config, "SERVICE_TOKEN_FILENAME", str(token_path))
    monkeypatch.setattr(incluster_config, "SERVICE_CERT_FILENAME", str(cert_path))
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    configuration = incluster.client.Configuration()
    incluster_config.load_incluster_config(client_configuration=configuration)
    assert set(configuration.api_key) == {"BearerToken"}
    incluster.verify_service_account(configuration, settings()["kubernetes"])


async def test_service_account_only_mode_never_initializes_aws(bootstrap: dict[str, Any]) -> None:
    executor = incluster.build_executor(settings(), NamespacePolicy(("payment",), ("kube-*",)))
    try:
        await executor.prepare()
        assert "k8s" in executor.capabilities()
        assert "aws" not in executor.capabilities()
        assert bootstrap["sessions"] == 0
        assert bootstrap["aws"].calls == []
        assert bootstrap["configuration"].host == "https://10.0.0.1:443"
    finally:
        await executor.close()


async def test_pod_identity_is_checked_before_loading_sa_and_pins_eks_endpoint(
    bootstrap: dict[str, Any],
) -> None:
    executor = incluster.build_executor(settings(aws=True), NamespacePolicy(("payment",), ()))
    try:
        assert bootstrap["aws"].calls[0][0] == "get_caller_identity"
        assert bootstrap["aws"].calls[1][0] == "describe_cluster"
        assert bootstrap["configuration"].host == ENDPOINT
        assert bootstrap["loaded"] == 1
        await executor.prepare()
        assert "aws" in executor.capabilities()
        bootstrap["api"].calls.clear()
        bootstrap["aws"].identity["Arn"] = "arn:aws:sts::123456789012:assumed-role/admin/test"
        with pytest.raises(IdentityRejected):
            await executor.prepare()
        assert bootstrap["api"].calls == []
    finally:
        await executor.close()


@pytest.mark.parametrize(
    "case", ["admin", "account", "provider", "pattern", "ca", "cluster", "tls", "subject"]
)
def test_incluster_guard_rejects_wrong_identity_before_cluster_reads(
    bootstrap: dict[str, Any], case: str
) -> None:
    cfg = settings(aws=True)
    if case == "admin":
        bootstrap["aws"].identity["Arn"] = "arn:aws:sts::123456789012:assumed-role/admin/test"
    elif case == "account":
        bootstrap["aws"].identity["Account"] = "999999999999"
    elif case == "provider":
        bootstrap["method"] = "iam-role"
    elif case == "pattern":
        cfg["aws"]["expected_role_arn_pattern"] = "arn:aws:sts::123456789012:assumed-role/*/*"
    elif case == "ca":
        bootstrap["ca"].write_bytes(b"another-cluster-ca")
    elif case == "cluster":
        bootstrap["aws"].responses["describe_cluster"]["cluster"]["arn"] = (
            "arn:aws:eks:test-region:123456789012:cluster/another-cluster"
        )
    elif case == "tls":
        bootstrap["verify_ssl"] = False
    else:
        bootstrap["subject"] = "system:serviceaccount:kube-system:admin"
    with pytest.raises(IdentityRejected):
        incluster.build_executor(cfg, NamespacePolicy())
    assert bootstrap["api_created"] == 0
    assert bootstrap["api"].calls == []
    if case in {"admin", "account", "provider", "pattern"}:
        assert bootstrap["loaded"] == 0


@pytest.mark.parametrize("name", ["KUBECONFIG", *incluster._LOCAL_CREDENTIAL_ENV])
def test_incluster_rejects_laptop_and_static_credential_overrides(
    bootstrap: dict[str, Any], monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, "synthetic-override")
    with pytest.raises(IdentityRejected):
        incluster.build_executor(settings(aws=True), NamespacePolicy())
    assert bootstrap["sessions"] == 0
    assert bootstrap["api_created"] == 0


async def test_incluster_cli_uses_same_policy_projection_and_redaction_pipeline(
    bootstrap: dict[str, Any],
) -> None:
    spoke = build_client(settings(), token="synthetic-spoke-token", fixture_root="unused")
    try:
        assert spoke._prepare is not None
        await spoke._prepare()
        good = await spoke.pipeline.execute(
            "pods", "get_pods", {"namespace": "payments"}, deadline_ms=1000
        )
        assert good.ok
        assert "hunter2" not in json.dumps(good.data)
        before = len(bootstrap["api"].calls)
        denied = await spoke.pipeline.execute(
            "deny", "get_pods", {"namespace": "kube-system"}, deadline_ms=1000
        )
        assert not denied.ok and denied.error is not None
        assert denied.error.code.value == "NOT_ALLOWED"
        assert len(bootstrap["api"].calls) == before
    finally:
        await spoke.close()
        assert isinstance(spoke.pipeline.executor, LocalExecutor)
        await spoke.pipeline.executor.close()


@pytest.mark.parametrize(
    "url",
    [
        "ws://public.example.com:8765/spoke/v1/connect",
        "wss://user:password@hub.example.com/spoke/v1/connect",
        "wss://hub.example.com/spoke/v1/connect?token=synthetic",
        "wss://hub.example.com/another-path",
    ],
)
def test_incluster_transport_rejects_public_plaintext_and_url_credentials(
    bootstrap: dict[str, Any], url: str
) -> None:
    cfg = settings()
    cfg["hub"]["url"] = url
    with pytest.raises(RuntimeError):
        build_client(cfg, token="synthetic-token", fixture_root="unused")
    assert bootstrap["loaded"] == 0
    assert bootstrap["sessions"] == 0
