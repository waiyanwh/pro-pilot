"""Spoke-side namespace enforcement."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Any

from pro_pilot.protocol.tools import TOOL_REGISTRY

DEFAULT_DENY = (
    "kube-*",
    "argocd",
    "argo-rollouts",
    "karpenter",
    "datadog*",
    "hoop*",
    "cert-manager",
    "external-secrets",
    "kubecost",
    "default",
)


@dataclass(frozen=True)
class NamespacePolicy:
    allow_patterns: tuple[str, ...] = ("*",)
    deny_patterns: tuple[str, ...] = DEFAULT_DENY

    def allowed(self, namespace: str) -> bool:
        """Return whether a namespace matches allow and does not match deny."""

        return any(
            fnmatch.fnmatchcase(namespace, pattern) for pattern in self.allow_patterns
        ) and not any(fnmatch.fnmatchcase(namespace, pattern) for pattern in self.deny_patterns)

    def denied_message(self, namespace: str) -> str:
        return f"namespace {namespace!r} is not allowed on this cluster"

    def check_tool_args(self, tool_name: str, args: dict[str, Any]) -> str | None:
        """Return a denied namespace, if this call has a namespace scope."""

        definition = TOOL_REGISTRY[tool_name]
        namespace = args.get("namespace")
        if definition.namespaced and isinstance(namespace, str) and not self.allowed(namespace):
            return namespace
        return None

    def filter_items(self, tool_name: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Filter cluster-wide list results without exposing denied objects or counts."""

        if tool_name not in {"list_namespaces", "list_applications"}:
            return [
                item
                for item in items
                if not isinstance(item.get("namespace"), str) or self.allowed(item["namespace"])
            ]
        if tool_name == "list_namespaces":
            return [
                item
                for item in items
                if isinstance(item.get("name"), str) and self.allowed(item["name"])
            ]
        return [
            item
            for item in items
            if isinstance(item.get("destination_namespace"), str)
            and self.allowed(item["destination_namespace"])
        ]

    def application_allowed(self, data: dict[str, Any]) -> bool:
        namespace = data.get("destination_namespace")
        return isinstance(namespace, str) and self.allowed(namespace)
