"""Read-only Argo CD Application status projections."""

from __future__ import annotations

from typing import Any

from pro_pilot.protocol.messages import ErrorCode
from pro_pilot.spoke.execution import mark_truncated
from pro_pilot.spoke.pipeline import ToolFailure
from pro_pilot.spoke.policy import NamespacePolicy
from pro_pilot.spoke.tools.common import KubeReader, parse_time, timestamp


class ArgoTools:
    def __init__(
        self, reader: KubeReader, policy: NamespacePolicy, *, argocd_namespace: str = "argocd"
    ) -> None:
        self.reader = reader
        self.policy = policy
        self.argocd_namespace = argocd_namespace

    def execute(self, tool_name: str, args: dict[str, Any]) -> Any:
        version = self.reader.api_versions["argoproj.io"]
        if tool_name == "list_applications":
            applications = self.reader.list(
                version, "applications", namespace=self.argocd_namespace
            )
            result = []
            for application in applications:
                if not self._allowed(application):
                    continue
                row = self._summary(application)
                if args.get("name_contains") and args["name_contains"] not in row["name"]:
                    continue
                if args.get("sync") and args["sync"] != row["sync"]:
                    continue
                if args.get("health") and args["health"] != row["health"]:
                    continue
                result.append(row)
            return result
        if tool_name == "get_application":
            application = self.reader.get(
                version, "applications", namespace=self.argocd_namespace, name=args["name"]
            )
            if not self._allowed(application):
                raise ToolFailure(ErrorCode.NOT_ALLOWED, "application destination is not allowed")
            return self._details(application)
        raise ValueError("unsupported Argo CD tool")

    def _allowed(self, application: dict[str, Any]) -> bool:
        namespace = application.get("spec", {}).get("destination", {}).get("namespace")
        return isinstance(namespace, str) and bool(namespace) and self.policy.allowed(namespace)

    @staticmethod
    def _summary(application: dict[str, Any]) -> dict[str, Any]:
        spec = application.get("spec", {})
        status = application.get("status", {})
        operation = status.get("operationState") or {}
        revision = status.get("sync", {}).get("revision")
        return {
            "name": application["metadata"]["name"],
            "project": spec.get("project"),
            "destination_namespace": spec.get("destination", {}).get("namespace"),
            "sync": status.get("sync", {}).get("status"),
            "health": status.get("health", {}).get("status"),
            "revision_short": revision[:7] if isinstance(revision, str) else None,
            "last_sync": {
                "phase": operation.get("phase"),
                "finished_at": timestamp(operation.get("finishedAt")),
            }
            if operation
            else None,
        }

    def _details(self, application: dict[str, Any]) -> dict[str, Any]:
        result = self._summary(application)
        status = application.get("status", {})
        operation = status.get("operationState") or {}
        resources = []
        for resource in status.get("resources", []):
            namespace = resource.get("namespace")
            # An Application's allowed destination does not authorize its other resources.
            if (
                not isinstance(namespace, str)
                or not namespace
                or not self.policy.allowed(namespace)
            ):
                continue
            health = resource.get("health") or {}
            if resource.get("status") == "Synced" and health.get("status", "Healthy") == "Healthy":
                continue
            resources.append(
                {
                    "kind": resource.get("kind"),
                    "namespace": namespace,
                    "name": resource.get("name"),
                    "sync": resource.get("status"),
                    "health": health.get("status"),
                    "message": health.get("message"),
                }
            )
        history = sorted(
            status.get("history", []),
            key=lambda row: (
                parsed.timestamp()
                if (parsed := parse_time(row.get("deployedAt")))
                else float("-inf")
            ),
            reverse=True,
        )
        condition_rows = status.get("conditions", [])
        resource_cap = min(50, self.reader.max_items)
        history_cap = min(5, self.reader.max_items)
        if (
            len(resources) > resource_cap
            or len(history) > history_cap
            or len(condition_rows) > self.reader.max_items
        ):
            mark_truncated()
        result.update(
            {
                "conditions": [
                    {"type": row.get("type"), "message": row.get("message")}
                    for row in condition_rows[: self.reader.max_items]
                ],
                "operation_state": {
                    "phase": operation.get("phase"),
                    "message": operation.get("message"),
                    "started_at": timestamp(operation.get("startedAt")),
                    "finished_at": timestamp(operation.get("finishedAt")),
                }
                if operation
                else None,
                "unhealthy_resources": resources[:resource_cap],
                "history": [
                    {
                        "revision_short": row["revision"][:7]
                        if isinstance(row.get("revision"), str)
                        else None,
                        "deployed_at": timestamp(row.get("deployedAt")),
                    }
                    for row in history[:history_cap]
                ],
            }
        )
        return result
