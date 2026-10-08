"""Read-only Rollout and AnalysisRun status projections."""

from __future__ import annotations

from typing import Any

from pro_pilot.protocol.messages import ErrorCode
from pro_pilot.spoke.execution import mark_truncated
from pro_pilot.spoke.pipeline import ToolFailure
from pro_pilot.spoke.policy import NamespacePolicy
from pro_pilot.spoke.tools.common import (
    KubeReader,
    images,
    owned_by,
    parse_time,
    selector,
    timestamp,
)


class RolloutTools:
    def __init__(self, reader: KubeReader, policy: NamespacePolicy) -> None:
        self.reader = reader
        self.policy = policy

    def execute(self, tool_name: str, args: dict[str, Any]) -> Any:
        if tool_name not in {"get_rollout", "list_analysis_runs"}:
            raise ValueError("unsupported Rollouts tool")
        namespace = args["namespace"]
        if not self.policy.allowed(namespace):
            raise ToolFailure(ErrorCode.NOT_ALLOWED, self.policy.denied_message(namespace))
        if tool_name == "get_rollout":
            return self._get_rollout(namespace, args["name"])
        return self._list_analysis_runs(namespace, args.get("rollout"), args.get("limit", 5))

    def _get_rollout(self, namespace: str, name: str) -> dict[str, Any]:
        rollout = self.reader.get(
            self.reader.api_versions["argoproj.io"], "rollouts", namespace=namespace, name=name
        )
        spec, status = rollout.get("spec", {}), rollout.get("status", {})
        strategy = spec.get("strategy", {})
        canary = "canary" in strategy
        strategy_name = "canary" if canary else "blueGreen" if "blueGreen" in strategy else None
        replica_sets = self.reader.list(
            "apps/v1",
            "replicasets",
            namespace=namespace,
            params={"labelSelector": selector(spec.get("selector", {}))},
        )
        by_hash = {
            row.get("metadata", {}).get("labels", {}).get("rollouts-pod-template-hash"): row
            for row in replica_sets
            if owned_by(row, kind="Rollout", name=name)
        }
        blue_green = status.get("blueGreen") or {}
        stable_hash = status.get("stableRS") or blue_green.get("activeSelector")
        current_hash = status.get("currentPodHash") or blue_green.get("previewSelector")

        def rs_summary(rs_hash: str | None) -> dict[str, Any] | None:
            if not rs_hash:
                return None
            row = by_hash.get(rs_hash, {})
            return {"hash": rs_hash, "images": images(row.get("spec", {}).get("template", {}))}

        steps = strategy.get("canary", {}).get("steps", [])
        index = status.get("currentStepIndex") or 0
        current_step = f"{min(index + 1, len(steps))}/{len(steps)}" if canary and steps else None
        weight = status.get("canary", {}).get("weights", {}).get("canary", {}).get("weight")
        if weight is None and canary:
            for step in steps[: index + 1]:
                if "setWeight" in step:
                    weight = step["setWeight"]
        pauses = status.get("pauseConditions", [])
        paused = (
            {"reason": pauses[0].get("reason")}
            if pauses
            else ({"reason": "UserPause"} if spec.get("paused") else None)
        )
        analyses = []
        for state in (status.get("canary") or {}, blue_green):
            for key in (
                "currentStepAnalysisRunStatus",
                "currentBackgroundAnalysisRunStatus",
                "prePromotionAnalysisRunStatus",
                "postPromotionAnalysisRunStatus",
            ):
                analysis_name = (state.get(key) or {}).get("name")
                if isinstance(analysis_name, str) and analysis_name not in analyses:
                    analyses.append(analysis_name)
        return {
            "phase": status.get("phase"),
            "message": status.get("message"),
            "strategy": strategy_name,
            "current_step": current_step,
            "set_weight": weight if canary else None,
            "paused": paused,
            "aborted": bool(status.get("abort")),
            "stable_rs": rs_summary(stable_hash),
            "canary_rs": rs_summary(current_hash) if current_hash != stable_hash else None,
            "replicas": {
                "desired": spec.get("replicas", 1),
                "ready": status.get("readyReplicas", 0),
                "updated": status.get("updatedReplicas", 0),
                "available": status.get("availableReplicas", 0),
            },
            "current_analysis_runs": analyses,
        }

    def _list_analysis_runs(
        self, namespace: str, rollout: str | None, limit: int
    ) -> list[dict[str, Any]]:
        runs = self.reader.list(
            self.reader.api_versions["argoproj.io"], "analysisruns", namespace=namespace
        )
        if rollout:
            runs = [row for row in runs if owned_by(row, kind="Rollout", name=rollout)]
        runs.sort(
            key=lambda row: (
                started.timestamp()
                if (
                    started := (
                        parse_time(row.get("status", {}).get("startedAt"))
                        or parse_time(row.get("metadata", {}).get("creationTimestamp"))
                    )
                )
                else float("-inf")
            ),
            reverse=True,
        )
        if len(runs) > limit:
            mark_truncated()
        result = []
        for run in runs[:limit]:
            status = run.get("status", {})
            metrics = []
            metric_rows = status.get("metricResults", [])
            if len(metric_rows) > self.reader.max_items:
                mark_truncated()
            for metric in metric_rows[: self.reader.max_items]:
                measurements = metric.get("measurements", [])
                measurement = measurements[-1] if measurements else None
                metrics.append(
                    {
                        "name": metric.get("name"),
                        "phase": metric.get("phase"),
                        "successful": metric.get("successful", 0),
                        "failed": metric.get("failed", 0),
                        "inconclusive": metric.get("inconclusive", 0),
                        "error": metric.get("error", 0),
                        "last_measurement": {
                            "value": measurement.get("value"),
                            "message": measurement.get("message"),
                            "finished_at": timestamp(measurement.get("finishedAt")),
                        }
                        if measurement
                        else None,
                    }
                )
            result.append(
                {
                    "name": run["metadata"]["name"],
                    "phase": status.get("phase"),
                    "started_at": timestamp(status.get("startedAt")),
                    "message": status.get("message"),
                    "metrics": metrics,
                }
            )
        return result
