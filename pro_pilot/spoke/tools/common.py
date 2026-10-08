"""Bounded Kubernetes GET transport and small shared projection helpers."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any

from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import ConnectTimeoutError, ReadTimeoutError

from pro_pilot.spoke.execution import mark_truncated, remaining_seconds

_RESOURCES = {
    "": {"namespaces", "nodes", "pods", "events", "services", "endpoints"},
    "apps": {"deployments", "replicasets", "statefulsets", "daemonsets"},
    "batch": {"jobs", "cronjobs"},
    "autoscaling": {"horizontalpodautoscalers"},
    "networking.k8s.io": {"ingresses"},
    "metrics.k8s.io": {"pods", "nodes"},
    "argoproj.io": {"applications", "rollouts", "analysisruns"},
    "karpenter.sh": {"nodeclaims", "nodepools"},
}
_MAX_RAW_BYTES = 8 * 1024 * 1024


class KubeReader:
    def __init__(self, api_client: Any, *, max_items: int = 200, timeout_s: float = 15) -> None:
        self.api_client = api_client
        self.max_items = max_items
        self.timeout_s = timeout_s
        self.api_versions = {
            "metrics.k8s.io": "metrics.k8s.io/v1beta1",
            "argoproj.io": "argoproj.io/v1alpha1",
            "karpenter.sh": "karpenter.sh/v1",
        }

    def get(
        self,
        api_version: str,
        resource: str,
        *,
        namespace: str | None = None,
        name: str | None = None,
        subresource: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        group = "" if api_version == "v1" else api_version.rsplit("/", 1)[0]
        if resource not in _RESOURCES.get(group, set()):
            raise ValueError("resource is outside the spoke read catalogue")
        prefix = "/api/v1" if api_version == "v1" else f"/apis/{api_version}"
        path_params: dict[str, str] = {}
        path = prefix
        if namespace is not None:
            path += "/namespaces/{namespace}"
            path_params["namespace"] = namespace
        path += f"/{resource}"
        if name is not None:
            path += "/{name}"
            path_params["name"] = name
        if subresource is not None:
            if subresource != "log" or resource != "pods" or name is None:
                raise ValueError("only named pod log subresources are readable")
            path += "/log"
        return self._get(path, path_params=path_params, params=params, text=subresource == "log")

    def list(
        self,
        api_version: str,
        resource: str,
        *,
        namespace: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        query = {"limit": self.max_items, **(params or {})}
        query["limit"] = min(self.max_items, max(1, int(query["limit"])))
        payload = self.get(api_version, resource, namespace=namespace, params=query)
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise RuntimeError("Kubernetes list returned an invalid object")
        items = payload["items"]
        if payload.get("metadata", {}).get("continue") or len(items) > query["limit"]:
            mark_truncated()
        return items[: query["limit"]]

    def discover(self) -> dict[str, dict[str, Any]]:
        groups = self._get("/apis")
        discovered: dict[str, dict[str, Any]] = {}
        for group in groups.get("groups", []):
            name = group.get("name")
            if name not in self.api_versions:
                continue
            versions = {row["groupVersion"] for row in group.get("versions", [])}
            preferred = self.api_versions[name]
            if preferred not in versions:
                preferred = group.get("preferredVersion", {}).get("groupVersion")
            if not preferred:
                continue
            self.api_versions[name] = preferred
            try:
                resources = self._get(f"/apis/{preferred}")
            except (PermissionError, FileNotFoundError):
                continue
            discovered[name] = {
                row["name"]: row
                for row in resources.get("resources", [])
                if "name" in row and "/" not in row["name"]
            }
        return discovered

    def _get(
        self,
        path: str,
        *,
        path_params: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        text: bool = False,
    ) -> Any:
        remaining = remaining_seconds(self.timeout_s)
        until = time.monotonic() + remaining
        try:
            response = self.api_client.call_api(
                path,
                "GET",
                path_params=path_params or {},
                query_params=list((params or {}).items()),
                header_params={"Accept": "text/plain" if text else "application/json"},
                response_types_map={200: "str" if text else "object"},
                auth_settings=["BearerToken"],
                _return_http_data_only=True,
                _preload_content=False,
                _request_timeout=(min(3, remaining), remaining),
            )
            body = bytearray()
            try:
                for chunk in response.stream(65536):
                    if time.monotonic() >= until:
                        raise TimeoutError("Kubernetes read timed out")
                    if len(body) + len(chunk) > _MAX_RAW_BYTES:
                        raise RuntimeError("Kubernetes response exceeds the raw byte limit")
                    body.extend(chunk)
                return body.decode("utf-8", errors="replace") if text else json.loads(body)
            finally:
                response.close()
                response.release_conn()
        except ApiException as exc:
            if exc.status == 403:
                raise PermissionError("Kubernetes read access denied") from exc
            if exc.status == 404:
                raise FileNotFoundError("Kubernetes resource not found") from exc
            if exc.status in {408, 504}:
                raise TimeoutError("Kubernetes read timed out") from exc
            raise RuntimeError(f"Kubernetes read failed (HTTP {exc.status})") from exc
        except (ReadTimeoutError, ConnectTimeoutError) as exc:
            raise TimeoutError("Kubernetes read timed out") from exc

    def close(self) -> None:
        self.api_client.close()


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def timestamp(value: Any) -> str | None:
    parsed = parse_time(value)
    return parsed.isoformat().replace("+00:00", "Z") if parsed else None


def age(value: Any) -> str | None:
    parsed = parse_time(value)
    if parsed is None:
        return None
    seconds = max(0, int((utc_now() - parsed).total_seconds()))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def conditions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": row.get("type"),
            "status": row.get("status"),
            "reason": row.get("reason"),
            "message": row.get("message"),
            "last_transition_time": timestamp(row.get("lastTransitionTime")),
        }
        for row in rows
    ]


def images(template: dict[str, Any]) -> list[str]:
    return [
        row["image"] for row in template.get("spec", {}).get("containers", []) if "image" in row
    ]


def selector(value: dict[str, Any]) -> str:
    parts = [f"{key}={item}" for key, item in sorted(value.get("matchLabels", {}).items())]
    for expression in value.get("matchExpressions", []):
        key = expression["key"]
        operator = expression["operator"]
        if operator in {"In", "NotIn"}:
            parts.append(
                f"{key} {'in' if operator == 'In' else 'notin'} "
                f"({','.join(expression.get('values', []))})"
            )
        elif operator == "Exists":
            parts.append(key)
        elif operator == "DoesNotExist":
            parts.append(f"!{key}")
        else:
            raise ValueError("unsupported workload selector operator")
    return ",".join(parts)


def owned_by(item: dict[str, Any], *, kind: str, name: str) -> bool:
    return any(
        row.get("kind") == kind and row.get("name") == name
        for row in item.get("metadata", {}).get("ownerReferences", [])
    )
