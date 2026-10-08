"""Explicitly opt-in live golden-set runs; ordinary tests never invoke this path."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import time
from dataclasses import replace
from pathlib import Path
from string import Template
from typing import Any

import yaml

from pro_pilot.hub.main import build_components
from pro_pilot.hub.main import load_config as load_hub_config
from pro_pilot.hub.registry import SpokeRegistry
from pro_pilot.spoke.local import LocalExecutor
from pro_pilot.spoke.main import build_client
from pro_pilot.spoke.main import load_config as load_spoke_config
from pro_pilot.spoke.policy import DEFAULT_DENY, NamespacePolicy
from pro_pilot.spoke.redact import redact


async def run_cases(
    hub_path: str,
    spoke_path: str,
    cases_path: str,
    variables: dict[str, str],
    output: str,
) -> None:
    with Path(cases_path).open(encoding="utf-8") as handle:
        cases = yaml.safe_load(handle)
    if not isinstance(cases, list) or not cases:
        raise ValueError("golden cases must be a nonempty list")
    for case in cases:
        Template(case["question"]).substitute(variables)
    hub_config = replace(load_hub_config(hub_path), spoke_host="127.0.0.1", spoke_port=0)
    if hub_config.llm_mode == "fake":
        raise ValueError("live golden-set scoring requires a real selected model")
    spoke_config = load_spoke_config(spoke_path)
    spoke_config["mode"] = "local"
    policy = NamespacePolicy(
        tuple(spoke_config.get("namespaces", {}).get("allow", ["*"])),
        tuple(spoke_config.get("namespaces", {}).get("deny", DEFAULT_DENY)),
    )
    executor = await asyncio.to_thread(LocalExecutor.from_config, spoke_config, policy)
    cluster_id = str(spoke_config["cluster_id"])
    token = secrets.token_urlsafe(32)
    original = os.environ.get("PRO_PILOT_SPOKE_TOKENS_JSON")
    os.environ["PRO_PILOT_SPOKE_TOKENS_JSON"] = json.dumps(
        {cluster_id: [SpokeRegistry.hash_token(token)]}
    )
    try:
        registry, server, handler = build_components(hub_config)
    except BaseException:
        await executor.close()
        if original is None:
            os.environ.pop("PRO_PILOT_SPOKE_TOKENS_JSON", None)
        else:
            os.environ["PRO_PILOT_SPOKE_TOKENS_JSON"] = original
        raise
    histories: dict[str, list[dict[str, Any]]] = {}
    records = []
    await server.start()
    port = server._server.sockets[0].getsockname()[1]
    fixture_root = (
        Path(cases_path).resolve().parents[0].parent / "fixtures" / "clusters" / "demo-cluster"
    )
    try:
        for case in cases:
            current = {**spoke_config, "hub": {"url": f"ws://127.0.0.1:{port}/spoke/v1/connect"}}
            fixture = case.get("fixture")
            if fixture:
                current.update(
                    mode="fake",
                    fixture_scenario=fixture,
                    namespaces={"allow": ["payment"], "deny": list(DEFAULT_DENY)},
                )
            spoke = build_client(
                current,
                token=token,
                fixture_root=fixture_root,
                executor=None if fixture else executor,
            )
            connected = asyncio.Event()
            original_register = registry.register

            async def register(
                hello: Any,
                websocket: Any,
                original: Any = original_register,
                ready_event: asyncio.Event = connected,
            ) -> Any:
                value = await original(hello, websocket)
                ready_event.set()
                return value

            registry.register = register
            task = asyncio.create_task(spoke.run_once())
            try:
                ready = asyncio.create_task(connected.wait())
                done, _ = await asyncio.wait(
                    {ready, task}, timeout=30, return_when=asyncio.FIRST_COMPLETED
                )
                if task in done:
                    await task
                    raise RuntimeError("spoke closed before evaluation connection")
                if ready not in done:
                    ready.cancel()
                    raise TimeoutError("spoke evaluation connection timed out")
                question = Template(case["question"]).substitute(variables)
                thread = str(case.get("thread", case["id"]))
                start = time.monotonic()
                result = await handler.agent.run(
                    question, cluster_ids=[cluster_id], history=histories.get(thread, [])
                )
                elapsed = (time.monotonic() - start) * 1000
                histories[thread] = result.messages
                required = [
                    Template(value).substitute(variables) for value in case.get("must_mention", [])
                ]
                forbidden = case.get("forbidden", [])
                row = {
                    "id": case["id"],
                    "question": question,
                    "answer": result.text,
                    "outcome": result.outcome,
                    "duration_ms": elapsed,
                    "required_fact_check": all(
                        value.lower() in result.text.lower() for value in required
                    ),
                    "forbidden_content_check": not any(
                        value.lower() in result.text.lower() for value in forbidden
                    ),
                    "tool_trace": [
                        {
                            "tool": item.tool,
                            "args": item.args,
                            "result": item.result.model_dump(mode="json"),
                        }
                        for item in result.tools
                    ],
                    "human_score": None,
                }
                safe, _ = redact(row)
                records.append(safe)
                print(
                    f"{case['id']}: {result.outcome}, {elapsed / 1000:.2f}s; human review pending"
                )
            finally:
                if not ready.done():
                    ready.cancel()
                await asyncio.gather(ready, return_exceptions=True)
                registry.register = original_register
                await spoke.close()
                await asyncio.gather(task, return_exceptions=True)
        destination = Path(output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps({"model": hub_config.model_id, "cases": records}, indent=2), encoding="utf-8"
        )
        durations = sorted(record["duration_ms"] for record in records)
        print(
            f"Saved {len(records)} redacted transcripts to {destination}; "
            f"p50={durations[(len(durations) - 1) // 2] / 1000:.2f}s, "
            f"p95={durations[min(len(durations) - 1, int(len(durations) * 0.95))] / 1000:.2f}s. "
            "Human scoring required; these checks are not a quality pass."
        )
    finally:
        await server.stop()
        await executor.close()
        if callable(getattr(handler.agent.llm, "close", None)):
            await handler.agent.llm.close()
        if original is None:
            os.environ.pop("PRO_PILOT_SPOKE_TOKENS_JSON", None)
        else:
            os.environ["PRO_PILOT_SPOKE_TOKENS_JSON"] = original


def main() -> None:
    parser = argparse.ArgumentParser(description="Operator-only live Phase 1 golden-set run")
    parser.add_argument(
        "--live",
        action="store_true",
        help="explicitly allow AWS, Kubernetes and selected LLM reads",
    )
    parser.add_argument("--hub-config", required=True)
    parser.add_argument("--spoke-config", required=True)
    parser.add_argument("--cases", default="tests/eval/golden.yaml")
    parser.add_argument("--var", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--output", default="tests/eval/out/phase1.json")
    args = parser.parse_args()
    if not args.live:
        parser.error(
            "--live is required; this runner contacts the configured infrastructure and model"
        )
    variables = dict(value.split("=", 1) for value in args.var)
    try:
        asyncio.run(
            run_cases(args.hub_config, args.spoke_config, args.cases, variables, args.output)
        )
    except Exception as exc:
        raise SystemExit(
            f"golden-set run failed ({type(exc).__name__}); "
            "check local configuration, role access and provider approval"
        ) from None
