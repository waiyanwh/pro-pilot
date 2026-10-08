"""Spoke entry point for fixture, guarded laptop and ServiceAccount execution."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from pro_pilot.hub.health import HealthServer
from pro_pilot.logging import configure_logging
from pro_pilot.protocol.messages import Hello, SpokeLimits
from pro_pilot.protocol.tools import TOOL_REGISTRY
from pro_pilot.runtime import run_with_shutdown

from .client import ProtocolUnsupported, SpokeClient
from .fake import FakeFixtureExecutor
from .pipeline import PipelineConfig, ToolPipeline
from .policy import DEFAULT_DENY, NamespacePolicy


class SpokeFileSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PRO_PILOT_", env_nested_delimiter="__")
    cluster_id: str
    labels: dict[str, str] = Field(default_factory=dict)
    mode: str = "fake"
    fixture_scenario: str = "crashloop-oom"
    hub: dict[str, Any] = Field(default_factory=dict)
    namespaces: dict[str, Any] = Field(default_factory=dict)
    limits: dict[str, Any] = Field(default_factory=dict)
    kubernetes: dict[str, Any] = Field(default_factory=dict)
    aws: dict[str, Any] = Field(default_factory=dict)
    health: dict[str, Any] = Field(default_factory=dict)
    heartbeat_s: int = 20

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return env_settings, init_settings


def load_config(path: str | Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise ValueError("spoke config must be a YAML object")
    return SpokeFileSettings(**payload).model_dump()


def build_client(
    config: dict[str, Any],
    *,
    token: str,
    fixture_root: str | Path,
    executor: Any | None = None,
) -> SpokeClient:
    mode = config.get("mode", "fake")
    if mode not in {"fake", "local", "in-cluster"}:
        raise RuntimeError("supported spoke modes are fake, local and in-cluster")
    cluster_id = str(config["cluster_id"])
    hub = config.get("hub", {})
    url = str(hub.get("url", "ws://127.0.0.1:8765/spoke/v1/connect"))
    if mode == "in-cluster":
        endpoint = urlsplit(url)
        internal_service = re.fullmatch(
            r"[a-z0-9-]+\.[a-z0-9-]+\.svc(?:\.cluster\.local)?", endpoint.hostname or ""
        )
        if (
            endpoint.scheme not in {"ws", "wss"}
            or not endpoint.hostname
            or (endpoint.scheme == "ws" and internal_service is None)
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.query
            or endpoint.fragment
            or endpoint.path != "/spoke/v1/connect"
        ):
            raise RuntimeError(
                "in-cluster spokes require WSS or an internal Kubernetes Service URL"
            )
    namespaces = config.get("namespaces", {})
    limits = config.get("limits", {})
    policy = NamespacePolicy(
        tuple(namespaces.get("allow", ["*"])),
        tuple(namespaces.get("deny", DEFAULT_DENY)),
    )
    if executor is None:
        if mode == "fake":
            executor = FakeFixtureExecutor(
                fixture_root, str(config.get("fixture_scenario", "crashloop-oom"))
            )
        elif mode == "local":
            from .local import LocalExecutor

            executor = LocalExecutor.from_config(config, policy)
        else:
            from .incluster import build_executor

            executor = build_executor(config, policy)

    async def prepare() -> None:
        await executor.prepare()
        pipeline.advertised_tools = set(executor.advertised_tools())
        hello.capabilities = executor.capabilities()
        hello.tools = sorted(pipeline.advertised_tools)

    pipeline = ToolPipeline(
        executor,
        policy,
        advertised_tools=set(executor.advertised_tools()),
        cluster_id=cluster_id,
        config=PipelineConfig(
            tool_timeout_s=float(limits.get("tool_timeout_s", 15)),
            max_result_bytes=int(limits.get("max_result_bytes", 32768)),
            max_list_items=int(limits.get("max_list_items", 200)),
            max_concurrent_tools=int(limits.get("max_concurrent_tools", 4)),
        ),
    )
    hello = Hello(
        cluster_id=cluster_id,
        spoke_version="0.1.0",
        labels={str(key): str(value) for key, value in config.get("labels", {}).items()},
        capabilities=executor.capabilities(),
        tools=sorted(set(executor.advertised_tools()) & (set(TOOL_REGISTRY) - {"list_clusters"})),
        limits=SpokeLimits(
            max_result_bytes=int(limits.get("max_result_bytes", 32768)),
            tool_timeout_s=float(limits.get("tool_timeout_s", 15)),
        ),
    )
    return SpokeClient(
        url=url,
        token=token,
        hello=hello,
        pipeline=pipeline,
        heartbeat_s=int(config.get("heartbeat_s", 20)),
        prepare=prepare if mode != "fake" else None,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="pro-pilot read-only spoke")
    parser.add_argument("--config", required=True)
    parser.add_argument("--mode", choices=["fake", "local", "in-cluster"], default=None)
    return parser


async def run(config_path: str, mode: str | None = None) -> None:
    config = load_config(config_path)
    if mode is not None:
        config["mode"] = mode
    token = os.environ.get("PRO_PILOT_SPOKE_TOKEN")
    if not token:
        raise RuntimeError("PRO_PILOT_SPOKE_TOKEN is required and must not be stored in YAML")
    repo_root = Path(config_path).resolve().parent.parent
    cluster_id = str(config["cluster_id"])
    fixture_root = os.environ.get(
        "PRO_PILOT_FIXTURE_ROOT",
        str(repo_root / "tests" / "fixtures" / "clusters" / cluster_id),
    )
    client = await asyncio.to_thread(build_client, config, token=token, fixture_root=fixture_root)
    listen = config.get("health", {}).get("listen")
    if listen is None and config.get("mode") == "in-cluster":
        listen = "0.0.0.0:8080"
    health = None
    try:
        if listen:
            host, port = str(listen).rsplit(":", 1)
            health = HealthServer(
                client.is_connected,
                host=host,
                port=int(port),
                disconnected_status="hub_disconnected",
            )
            await health.start()
        await client.run_forever()
    except ProtocolUnsupported as exc:
        raise SystemExit(str(exc)) from exc
    finally:
        if health is not None:
            await health.stop()
        await client.close()
        if config.get("mode") in {"local", "in-cluster"}:
            from .local import LocalExecutor

            if isinstance(client.pipeline.executor, LocalExecutor):
                await client.pipeline.executor.close()


def main() -> None:
    configure_logging()
    args = _parser().parse_args()
    try:
        asyncio.run(run_with_shutdown(run(args.config, args.mode)))
    except Exception as exc:
        logging.getLogger(__name__).error(
            "spoke_startup_failed", extra={"error_type": type(exc).__name__}
        )
        raise SystemExit(
            "spoke startup failed; verify read-only identity and local configuration"
        ) from None
