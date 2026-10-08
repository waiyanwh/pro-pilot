from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml

from pro_pilot.spoke import local
from pro_pilot.spoke.local import IdentityRejected, validate_kubeconfig, verify_identity
from pro_pilot.spoke.policy import NamespacePolicy
from tests.local_support import (
    ROLE_ARN,
    ROLE_PATTERN,
    FixtureApi,
    FixtureAWS,
    load_fixture,
    local_executor,
)

ENDPOINT = "https://eks.example.invalid"


def scoped_kubeconfig() -> dict[str, Any]:
    return {
        "contexts": [{"name": "read-only", "context": {"user": "reader", "cluster": "target"}}],
        "clusters": [{"name": "target", "cluster": {"server": ENDPOINT}}],
        "users": [
            {
                "name": "reader",
                "user": {
                    "exec": {
                        "command": "aws",
                        "args": [
                            "--region",
                            "test-region",
                            "eks",
                            "get-token",
                            "--cluster-name",
                            "test-cluster",
                            "--output",
                            "json",
                        ],
                        "env": [{"name": "AWS_PROFILE", "value": "test-reader"}],
                    }
                },
            }
        ],
    }


def check_config(config: dict[str, Any]) -> None:
    validate_kubeconfig(
        config,
        context="read-only",
        profile="test-reader",
        region="test-region",
        cluster_name="test-cluster",
        endpoint=ENDPOINT,
    )


@pytest.mark.parametrize(
    "arn",
    [
        "arn:aws:sts::123456789012:assumed-role/AdministratorAccess/test",
        "arn:aws:sts::999999999999:assumed-role/pro-pilot-read/test",
        "arn:aws:iam::123456789012:user/reader",
    ],
)
def test_identity_guard_rejects_admin_wrong_account_and_non_role(arn: str) -> None:
    sdk = FixtureAWS({})
    sdk.identity["Arn"] = arn
    with pytest.raises(IdentityRejected):
        verify_identity(sdk, ROLE_PATTERN)


def test_identity_guard_accepts_exact_read_role_and_rejects_wildcard_role() -> None:
    sdk = FixtureAWS({})
    assert verify_identity(sdk, ROLE_PATTERN)["Arn"] == ROLE_ARN
    calls = len(sdk.calls)
    with pytest.raises(IdentityRejected):
        verify_identity(sdk, "arn:aws:sts::123456789012:assumed-role/*/*")
    assert len(sdk.calls) == calls


@pytest.mark.parametrize(
    "case",
    ["profile", "cluster", "region", "endpoint", "tls", "token", "role_override", "environment"],
)
def test_kubeconfig_cannot_bypass_guarded_identity(case: str) -> None:
    config = scoped_kubeconfig()
    user = config["users"][0]["user"]
    execute = user["exec"]
    if case == "profile":
        execute["env"][0]["value"] = "admin"
    elif case == "cluster":
        execute["args"][6] = "another-cluster"
    elif case == "region":
        execute["args"][1] = "another-region"
    elif case == "endpoint":
        config["clusters"][0]["cluster"]["server"] = "https://other.example.invalid"
    elif case == "tls":
        config["clusters"][0]["cluster"]["insecure-skip-tls-verify"] = True
    elif case == "token":
        user["token"] = "synthetic-static-credential"
    elif case == "role_override":
        execute["args"].extend(["--role-arn", "arn:aws:iam::123456789012:role/admin"])
    else:
        execute["env"].append({"name": "AWS_ACCESS_KEY_ID", "value": "synthetic"})
    with pytest.raises(IdentityRejected):
        check_config(config)


def test_generated_kubeconfig_profile_is_accepted() -> None:
    check_config(scoped_kubeconfig())


async def test_capability_detection_advertises_only_discovered_apis_and_refreshes() -> None:
    fixture = load_fixture("k8s")
    fixture["routes"]["/apis"] = {
        "groups": [
            {
                "name": "metrics.k8s.io",
                "versions": [{"groupVersion": "metrics.k8s.io/v1beta1"}],
                "preferredVersion": {"groupVersion": "metrics.k8s.io/v1beta1"},
            }
        ]
    }
    fixture["routes"]["/apis/metrics.k8s.io/v1beta1"] = {"resources": [{"name": "pods"}]}
    executor = local_executor(fixture)
    executor.aws_enabled = False
    try:
        await executor.prepare()
        assert executor.capabilities() == ["k8s", "metrics"]
        assert "top_pods" in executor.advertised_tools()
        assert "get_application" not in executor.advertised_tools()
        executor.reader.api_client.routes["/apis"] = {"groups": []}
        await executor.prepare()
        assert executor.capabilities() == ["k8s"]
        assert "top_pods" not in executor.advertised_tools()
    finally:
        await executor.close()


async def test_identity_is_rechecked_before_reconnect_discovery() -> None:
    executor = local_executor(load_fixture("k8s"))
    executor.sts.identity["Arn"] = "arn:aws:sts::123456789012:assumed-role/admin/test"
    try:
        with pytest.raises(IdentityRejected):
            await executor.prepare()
        assert executor.reader.api_client.calls == []
    finally:
        await executor.close()


async def test_bootstrap_guards_profile_before_loading_cluster_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = load_fixture("k8s")
    aws = FixtureAWS({"describe_cluster": {"cluster": {"endpoint": ENDPOINT}}})
    api = FixtureApi(fixture["routes"])
    credentials_loaded = []

    class Session:
        def __init__(self, *, profile_name: str, region_name: str) -> None:
            assert profile_name == "test-reader"
            assert region_name == "test-region"

        def client(self, name: str, **kwargs: Any) -> Any:
            return aws

    class Loader:
        def __init__(self, payload: dict[str, Any], **kwargs: Any) -> None:
            assert aws.calls[0][0] == "get_caller_identity"
            execute = payload["users"][0]["user"]["exec"]
            assert execute["args"][-2:] == ["--profile", "test-reader"]

        def load_and_set(self, configuration: Any) -> None:
            credentials_loaded.append(True)

    monkeypatch.setattr(local.boto3, "Session", Session)
    monkeypatch.setattr(local, "KubeConfigLoader", Loader)
    monkeypatch.setattr(local.client, "ApiClient", lambda config: api)
    path = tmp_path / "synthetic-kubeconfig.yaml"
    path.write_text(yaml.safe_dump(scoped_kubeconfig()))
    settings = {
        "aws": {
            "profile": "test-reader",
            "region": "test-region",
            "eks_cluster_name": "test-cluster",
            "expected_role_arn_pattern": ROLE_PATTERN,
        },
        "kubernetes": {"context": "read-only", "kubeconfig": str(path)},
    }
    executor = local.LocalExecutor.from_config(settings, NamespacePolicy(("payment",), ()))
    await executor.close()
    assert credentials_loaded == [True]
    aws.identity["Arn"] = "arn:aws:sts::123456789012:assumed-role/admin/test"
    with pytest.raises(IdentityRejected):
        local.LocalExecutor.from_config(settings, NamespacePolicy(("payment",), ()))
    assert credentials_loaded == [True]


async def test_cancelled_sdk_read_keeps_bounded_worker_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = local_executor(load_fixture("k8s"))
    executor._slots = asyncio.Semaphore(1)
    started = threading.Event()
    release = threading.Event()
    calls = []

    def blocked(tool: str, args: dict[str, Any]) -> Any:
        calls.append(tool)
        started.set()
        release.wait(2)
        return []

    monkeypatch.setattr(executor._groups["k8s"], "execute", blocked)
    first = asyncio.create_task(executor.execute("get_pods", {"namespace": "payment"}))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(first, 0.2)
        second = asyncio.create_task(executor.execute("get_pods", {"namespace": "payment"}))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(second), 0.02)
        assert calls == ["get_pods"]
        release.set()
        assert (await asyncio.wait_for(second, 1)).data == []
    finally:
        release.set()
        await executor.close()
