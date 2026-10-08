from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from pro_pilot import evaluation
from pro_pilot.hub.main import build_components as assemble_hub
from tests.local_support import load_fixture, local_executor


async def test_unresolved_golden_variables_fail_before_infrastructure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cases = tmp_path / "cases.yaml"
    cases.write_text(yaml.safe_dump([{"id": "missing", "question": "Inspect $workload"}]))

    def unexpected(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("unresolved cases must not initialize infrastructure")

    monkeypatch.setattr(evaluation.LocalExecutor, "from_config", unexpected)
    with pytest.raises(KeyError):
        await evaluation.run_cases("unused", "unused", str(cases), {}, str(tmp_path / "out.json"))


class EvidenceModel:
    model_id = "offline-evaluation"

    async def converse(self, **request: Any) -> dict[str, Any]:
        latest = request["messages"][-1]["content"]
        if "toolResult" in latest[0]:
            evidence = latest[0]["toolResult"]["content"][0]["json"]
            content = [{"text": json.dumps(evidence)}]
            stop = "end_turn"
        else:
            content = [
                {
                    "toolUse": {
                        "toolUseId": "pods",
                        "name": "get_pods",
                        "input": {"cluster": request["cluster_ids"][0], "namespace": "payments"},
                    }
                }
            ]
            stop = "tool_use"
        return {
            "stopReason": stop,
            "output": {"message": {"role": "assistant", "content": content}},
        }


async def test_golden_runner_captures_safe_evidence_and_requires_human_score(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = load_fixture("k8s")
    fixture["routes"]["/apis"] = {"groups": []}
    executor = local_executor(fixture)
    executor.aws_enabled = False
    monkeypatch.setattr(evaluation.LocalExecutor, "from_config", lambda *args: executor)
    monkeypatch.setattr(
        evaluation,
        "build_components",
        lambda config: assemble_hub(config, llm_client=EvidenceModel()),
    )
    monkeypatch.setenv("PRO_PILOT_SPOKE_TOKENS_JSON", "{}")
    monkeypatch.delenv("PRO_PILOT_LLM_MODE", raising=False)
    hub = tmp_path / "hub.yaml"
    hub.write_text("llm:\n  mode: openai\n")
    spoke = tmp_path / "spoke.yaml"
    spoke.write_text("cluster_id: offline-golden\nmode: local\n")
    cases = tmp_path / "cases.yaml"
    cases.write_text(
        yaml.safe_dump(
            [
                {
                    "id": "evidence",
                    "question": "Inspect $workload",
                    "must_mention": ["OOMKilled"],
                }
            ]
        )
    )
    destination = tmp_path / "out.json"
    await evaluation.run_cases(
        str(hub), str(spoke), str(cases), {"workload": "api"}, str(destination)
    )
    record = json.loads(destination.read_text())["cases"][0]
    assert record["outcome"] == "answered"
    assert record["required_fact_check"]
    assert record["human_score"] is None
    assert record["tool_trace"][0]["tool"] == "get_pods"
    assert record["tool_trace"][0]["result"]["data"][0]["last_termination"]["reason"] == "OOMKilled"
    assert "hunter2" not in destination.read_text()
    assert evaluation.os.environ["PRO_PILOT_SPOKE_TOKENS_JSON"] == "{}"
