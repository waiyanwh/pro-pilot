"""Synthetic raw API responses used only by offline executor tests."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from kubernetes.client.exceptions import ApiException

from pro_pilot.spoke.local import LocalExecutor
from pro_pilot.spoke.policy import NamespacePolicy
from pro_pilot.spoke.tools.common import KubeReader

FIXTURES = Path(__file__).parent / "fixtures" / "local"
ROLE_PATTERN = "arn:aws:sts::123456789012:assumed-role/pro-pilot-read/*"
ROLE_ARN = "arn:aws:sts::123456789012:assumed-role/pro-pilot-read/test"


class RawResponse:
    def __init__(self, payload: Any) -> None:
        self.payload = (
            payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
        )

    def stream(self, size: int):
        for offset in range(0, len(self.payload), size):
            yield self.payload[offset : offset + size]

    def close(self) -> None:
        pass

    def release_conn(self) -> None:
        pass


class FixtureApi:
    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = copy.deepcopy(routes)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_api(self, path: str, method: str, **params: Any) -> RawResponse:
        if method != "GET":
            raise AssertionError("fixture API forbids mutations")
        for key, value in params.get("path_params", {}).items():
            path = path.replace("{" + key + "}", value)
        query = dict(params.get("query_params", []))
        self.calls.append((path, query))
        if path not in self.routes:
            raise ApiException(status=404, reason="recorded resource missing")
        response = copy.deepcopy(self.routes[path])
        if isinstance(response, dict) and "__status" in response:
            raise ApiException(status=response["__status"], reason="recorded read failure")
        return RawResponse(response)

    def close(self) -> None:
        pass


class FixtureAWS:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = copy.deepcopy(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.identity = {"Account": "123456789012", "Arn": ROLE_ARN}

    def _read(self, name: str, params: dict[str, Any]) -> Any:
        self.calls.append((name, params))
        if name == "get_caller_identity":
            return copy.deepcopy(self.identity)
        value = self.responses[name]
        if isinstance(value, list):
            # Sequences supply responses for repeated reads such as addons/groups.
            value = value.pop(0)
        return copy.deepcopy(value)

    def get_caller_identity(self, **params: Any) -> Any:
        return self._read("get_caller_identity", params)

    def describe_cluster(self, **params: Any) -> Any:
        return self._read("describe_cluster", params)

    def list_addons(self, **params: Any) -> Any:
        return self._read("list_addons", params)

    def describe_addon(self, **params: Any) -> Any:
        return self._read("describe_addon", params)

    def list_nodegroups(self, **params: Any) -> Any:
        return self._read("list_nodegroups", params)

    def describe_nodegroup(self, **params: Any) -> Any:
        return self._read("describe_nodegroup", params)

    def describe_load_balancers(self, **params: Any) -> Any:
        return self._read("describe_load_balancers", params)

    def describe_target_groups(self, **params: Any) -> Any:
        return self._read("describe_target_groups", params)

    def describe_target_health(self, **params: Any) -> Any:
        return self._read("describe_target_health", params)

    def get_metric_data(self, **params: Any) -> Any:
        return self._read("get_metric_data", params)

    def close(self) -> None:
        pass


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def local_executor(
    fixture: dict[str, Any], *, policy: NamespacePolicy | None = None
) -> LocalExecutor:
    api = FixtureApi(fixture["routes"])
    aws = FixtureAWS(fixture.get("aws", {}))
    executor = LocalExecutor(
        KubeReader(api),
        policy or NamespacePolicy(("payment", "payments"), ("kube-*", "argocd")),
        sts=aws,
        eks=aws,
        elbv2=aws,
        cloudwatch=aws,
        cluster_name="test-cluster",
        expected_role_arn_pattern=ROLE_PATTERN,
    )
    executor._capabilities = {"k8s", "metrics", "karpenter", "argocd", "rollouts", "aws"}
    return executor
