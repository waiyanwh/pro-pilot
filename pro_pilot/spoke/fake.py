"""Fixture-backed spoke execution for Phase 0."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class FakeFixtureError(LookupError):
    """A requested fixture or tool response is missing."""


class FakeFixtureExecutor:
    """Serve only recorded response data; no Kubernetes or AWS clients are imported."""

    def __init__(self, fixture_root: str | Path, scenario: str = "crashloop-oom") -> None:
        self.fixture_root = Path(fixture_root)
        self.scenario = scenario
        self.responses = self._load_responses()

    def _load_responses(self) -> dict[str, Any]:
        scenario_dir = self.fixture_root / self.scenario
        candidates = (scenario_dir / "responses.json", scenario_dir / "data.json")
        for path in candidates:
            if path.is_file():
                with path.open(encoding="utf-8") as handle:
                    payload = json.load(handle)
                if not isinstance(payload, dict):
                    raise ValueError(f"fixture file must contain an object: {path}")
                return payload.get("responses", payload)
        raise FakeFixtureError(f"fixture scenario not found: {self.scenario}")

    async def execute(self, tool_name: str, args: dict[str, Any]) -> Any:
        del args
        if tool_name not in self.responses:
            raise FakeFixtureError(f"fixture has no response for {tool_name}")
        return self.responses[tool_name]

    def advertised_tools(self) -> list[str]:
        return sorted(self.responses)

    def capabilities(self) -> list[str]:
        capabilities = {"k8s"}
        for tool_name in self.responses:
            if tool_name in {"top_pods"}:
                capabilities.add("metrics")
            elif tool_name in {"list_applications", "get_application"}:
                capabilities.add("argocd")
            elif tool_name in {"get_rollout", "list_analysis_runs"}:
                capabilities.add("rollouts")
            elif tool_name == "list_nodeclaims":
                capabilities.add("karpenter")
            elif tool_name in {
                "get_eks_cluster",
                "list_nodegroups",
                "get_alb_for_ingress",
                "get_alb_metrics",
            }:
                capabilities.add("aws")
        return sorted(capabilities)
