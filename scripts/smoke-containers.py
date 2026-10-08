#!/usr/bin/env python3
"""Offline image smoke: fake spoke, authenticated WebSocket, health and SIGTERM."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

HUB = """import asyncio, os
from aiohttp import web
from pro_pilot.hub.health import HealthServer
from pro_pilot.hub.main import HubConfig, build_components
from pro_pilot.hub.registry import SpokeRegistry
from pro_pilot.hub.slack_app import SlackConfig
from pro_pilot.runtime import run_with_shutdown

async def main():
    os.environ["PRO_PILOT_SPOKE_TOKENS_JSON"] = __import__("json").dumps(
        {"smoke-cluster": [SpokeRegistry.hash_token("synthetic-offline-token")]})
    registry, server, handler = build_components(HubConfig(
        slack=SlackConfig(channels={}), llm_mode="fake", spoke_host="0.0.0.0"))
    async def connected():
        return registry.get("smoke-cluster") is not None
    health = HealthServer(connected, host="0.0.0.0", port=8080,
                          disconnected_status="spoke_disconnected")
    async def answer(request):
        result = await handler.agent.run("Why is payment-api crashlooping?",
                                         cluster_ids=["smoke-cluster"])
        assert "OOMKilled" in result.text and "14" in result.text
        assert result.tools and all(record.result.ok for record in result.tools)
        return web.json_response({"status": "passed", "tool": result.tools[0].tool})
    health.app.router.add_get("/smoke", answer)
    try:
        await server.start()
        await health.start()
        await asyncio.Event().wait()
    finally:
        await health.stop()
        await server.stop()
        print("offline_hub_clean_shutdown", flush=True)
asyncio.run(run_with_shutdown(main()))
"""


def docker(*args: str) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def status(container: str, url: str) -> int:
    code = (
        "import urllib.request,urllib.error\n"
        "try:\n"
        f" with urllib.request.urlopen({url!r},timeout=2) as r: print(r.status)\n"
        "except urllib.error.HTTPError as e: print(e.code)\n"
        "except (urllib.error.URLError,TimeoutError): print(0)\n"
    )
    result = subprocess.run(
        ["docker", "exec", container, "python", "-c", code], capture_output=True, text=True
    )
    return int(result.stdout.strip()) if result.returncode == 0 else 0


def wait_ready(container: str, url: str) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if status(container, url + "/readyz") == 200:
            return
        time.sleep(0.2)
    raise RuntimeError("offline container readiness did not pass")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hub-image", default="pro-pilot-hub:0.1.0")
    parser.add_argument("--spoke-image", default="pro-pilot-spoke:0.1.0")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    prefix = "pro-pilot-smoke-" + uuid.uuid4().hex[:10]
    hub, spoke = prefix + "-hub", prefix + "-spoke"
    created: list[str] = []
    network_created = False
    try:
        with tempfile.TemporaryDirectory(prefix=prefix + "-") as scratch:
            path = Path(scratch)
            path.chmod(0o755)
            (path / "hub.py").write_text(HUB)
            fixtures = path / "fixtures"
            shutil.copytree(
                root / "tests/fixtures/clusters/demo-cluster/crashloop-oom",
                fixtures / "crashloop-oom",
            )
            (path / "config.yaml").write_text(
                "cluster_id: smoke-cluster\nmode: fake\n"
                "hub:\n  url: ws://hub:8765/spoke/v1/connect\n"
                "health:\n  listen: 0.0.0.0:8080\n"
            )
            docker("network", "create", "--internal", prefix)
            network_created = True
            common = (
                "--network",
                prefix,
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--tmpfs=/tmp:rw,nosuid,noexec",
                "--mount",
                f"type=bind,src={path},dst=/smoke,readonly",
            )
            docker(
                "run",
                "-d",
                "--name",
                hub,
                "--network-alias",
                "hub",
                *common,
                "--entrypoint",
                "python",
                args.hub_image,
                "/smoke/hub.py",
            )
            created.append(hub)
            docker(
                "run",
                "-d",
                "--name",
                spoke,
                *common,
                "--mount",
                f"type=bind,src={path / 'config.yaml'},dst=/etc/pro-pilot/config.yaml,readonly",
                "-e",
                "PRO_PILOT_SPOKE_TOKEN=synthetic-offline-token",
                "-e",
                "PRO_PILOT_FIXTURE_ROOT=/smoke/fixtures",
                args.spoke_image,
            )
            created.append(spoke)
            hub_url, spoke_url = "http://127.0.0.1:8080", f"http://{spoke}:8080"
            wait_ready(hub, hub_url)
            wait_ready(hub, spoke_url)
            assert status(hub, spoke_url + "/healthz") == 200
            answer = docker(
                "exec",
                hub,
                "python",
                "-c",
                "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080/smoke',timeout=10).read().decode())",
            )
            assert json.loads(answer)["status"] == "passed"
            docker("stop", "-t", "10", spoke)
            assert docker("inspect", "--format", "{{.State.ExitCode}}", spoke) == "0"
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and status(hub, hub_url + "/readyz") != 503:
                time.sleep(0.1)
            assert status(hub, hub_url + "/readyz") == 503
            assert status(hub, hub_url + "/healthz") == 200
            docker("stop", "-t", "10", hub)
            assert docker("inspect", "--format", "{{.State.ExitCode}}", hub) == "0"
            assert "offline_hub_clean_shutdown" in docker("logs", hub)
            print("Passed: authenticated fake spoke, tool result, probes, disconnect and SIGTERM.")
    finally:
        for name in reversed(created):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
        if network_created:
            subprocess.run(["docker", "network", "rm", prefix], capture_output=True, check=False)


if __name__ == "__main__":
    main()
