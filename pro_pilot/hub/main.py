"""Hub entry point for Slack Socket Mode and the spoke server."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from pro_pilot.logging import configure_logging
from pro_pilot.runtime import run_with_shutdown

from .agent import AgentConfig, AgentLoop
from .bedrock import BedrockClient
from .fake_bedrock import FakeBedrockClient
from .health import HealthServer
from .openai import OpenAIClient
from .policy import RateLimitConfig, RateLimiter
from .registry import SpokeRegistry
from .slack_app import SlackConfig, SlackHandler, build_slack_app
from .spoke_server import SpokeServer
from .thread_store import ThreadStore


class HubFileSettings(BaseSettings):
    """YAML defaults with PRO_PILOT_SECTION__KEY environment overrides."""

    model_config = SettingsConfigDict(env_prefix="PRO_PILOT_", env_nested_delimiter="__")
    slack: dict[str, Any] = Field(default_factory=dict)
    llm: dict[str, Any] = Field(default_factory=dict)
    agent: dict[str, Any] = Field(default_factory=dict)
    rate_limits: dict[str, Any] = Field(default_factory=dict)
    spoke_server: dict[str, Any] = Field(default_factory=dict)
    health: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Never read .env or secret files; the launcher supplies environment secrets.
        return env_settings, init_settings


@dataclass(frozen=True)
class HubConfig:
    slack: SlackConfig
    llm_mode: str = "openai"
    region: str = ""
    model_id: str = ""
    max_output_tokens: int = 2000
    temperature: float = 0
    base_url: str = ""
    agent: AgentConfig = AgentConfig()
    per_user_requests: int = 10
    per_user_s: float = 600
    global_requests: int = 60
    global_s: float = 600
    per_user_concurrent: int = 1
    thread_ttl_s: float = 86400
    thread_max_turns: int = 20
    spoke_host: str = "127.0.0.1"
    spoke_port: int = 8765
    heartbeat_s: int = 20
    health_host: str = "127.0.0.1"
    health_port: int = 8080


def load_config(path: str | Path) -> HubConfig:
    with Path(path).open(encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise ValueError("hub config must be a YAML object")
    settings = HubFileSettings(**payload)
    slack_data = settings.slack
    llm_data = settings.llm
    agent_data = settings.agent
    rate_data = settings.rate_limits
    server_data = settings.spoke_server
    return HubConfig(
        slack=SlackConfig(
            channels={
                str(channel): [str(cluster) for cluster in clusters]
                for channel, clusters in slack_data.get("channels", {}).items()
            },
            allow_dm=bool(slack_data.get("allow_dm", False)),
            max_question_chars=int(slack_data.get("max_question_chars", 1000)),
        ),
        llm_mode=str(os.environ.get("PRO_PILOT_LLM_MODE", llm_data.get("mode", "openai"))).lower(),
        region=str(llm_data.get("region", "")),
        model_id=str(llm_data.get("model_id", "")),
        max_output_tokens=int(llm_data.get("max_output_tokens", 2000)),
        temperature=float(llm_data.get("temperature", 0)),
        base_url=str(llm_data.get("base_url", "")),
        agent=AgentConfig(
            max_iterations=int(agent_data.get("max_iterations", 8)),
            max_tool_calls=int(agent_data.get("max_tool_calls", 16)),
            max_parallel_tool_calls=int(agent_data.get("max_parallel_tool_calls", 4)),
            turn_deadline_s=float(agent_data.get("turn_deadline_s", 90)),
            tool_result_chars_to_llm=int(agent_data.get("tool_result_chars_to_llm", 12000)),
        ),
        per_user_requests=int(rate_data.get("per_user", {}).get("requests", 10)),
        per_user_s=float(rate_data.get("per_user", {}).get("per_s", 600)),
        global_requests=int(rate_data.get("global", {}).get("requests", 60)),
        global_s=float(rate_data.get("global", {}).get("per_s", 600)),
        per_user_concurrent=int(rate_data.get("per_user_concurrent", 1)),
        thread_ttl_s=float(agent_data.get("thread_ttl_s", 86400)),
        thread_max_turns=int(agent_data.get("thread_max_turns", 20)),
        spoke_host=str(server_data.get("listen", "127.0.0.1:8765")).rsplit(":", 1)[0],
        spoke_port=int(str(server_data.get("listen", "127.0.0.1:8765")).rsplit(":", 1)[1]),
        heartbeat_s=int(server_data.get("heartbeat_s", 20)),
        health_host=str(settings.health.get("listen", "127.0.0.1:8080")).rsplit(":", 1)[0],
        health_port=int(str(settings.health.get("listen", "127.0.0.1:8080")).rsplit(":", 1)[1]),
    )


def build_components(
    config: HubConfig, *, llm_client: Any | None = None
) -> tuple[SpokeRegistry, SpokeServer, SlackHandler]:
    token_json = os.environ.get("PRO_PILOT_SPOKE_TOKENS_JSON", "{}")
    token_hashes = json.loads(token_json)
    if not isinstance(token_hashes, dict):
        raise ValueError("PRO_PILOT_SPOKE_TOKENS_JSON must be an object")
    registry = SpokeRegistry({str(key): list(value) for key, value in token_hashes.items()})
    if config.llm_mode not in {"fake", "bedrock", "openai"}:
        raise ValueError("llm.mode must be fake, bedrock or openai")
    if llm_client is not None:
        llm = llm_client
    elif config.llm_mode == "fake":
        llm = FakeBedrockClient()
    elif config.llm_mode == "openai":
        if not config.base_url or not config.model_id:
            raise ValueError("llm.base_url and llm.model_id are required for openai mode")
        llm = OpenAIClient(
            base_url=config.base_url,
            model_id=config.model_id,
            api_key=os.environ.get("PRO_PILOT_LLM_API_KEY", ""),
            max_output_tokens=config.max_output_tokens,
            temperature=config.temperature,
        )
    else:
        if not config.region or not config.model_id:
            raise ValueError("llm.region and llm.model_id are required for bedrock mode")
        llm = BedrockClient(
            region=config.region,
            model_id=config.model_id,
            max_output_tokens=config.max_output_tokens,
            temperature=config.temperature,
        )
    agent = AgentLoop(llm, registry, config=config.agent)
    limiter = RateLimiter(
        per_user=RateLimitConfig(config.per_user_requests, config.per_user_s),
        global_limit=RateLimitConfig(config.global_requests, config.global_s),
        max_concurrent_per_user=config.per_user_concurrent,
    )
    handler = SlackHandler(
        None,
        agent,
        config=config.slack,
        rate_limiter=limiter,
        thread_store=ThreadStore(
            ttl_s=config.thread_ttl_s,
            max_turns=config.thread_max_turns,
        ),
    )
    server = SpokeServer(
        registry,
        host=config.spoke_host,
        port=config.spoke_port,
        heartbeat_s=config.heartbeat_s,
    )
    return registry, server, handler


async def run(config_path: str | Path) -> None:
    config = load_config(config_path)
    if config.llm_mode == "fake":
        logging.getLogger(__name__).info(
            "LLM mode fake: no model API calls (Phase 0 local Slack + fixture spoke)"
        )
    bot_token = os.environ.get("PRO_PILOT_SLACK_BOT_TOKEN")
    app_token = os.environ.get("PRO_PILOT_SLACK_APP_TOKEN")
    if not bot_token or not app_token:
        raise RuntimeError("PRO_PILOT_SLACK_BOT_TOKEN and PRO_PILOT_SLACK_APP_TOKEN are required")
    _, server, handler = build_components(config)
    app = build_slack_app(handler, bot_token=bot_token)
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

    socket_handler = AsyncSocketModeHandler(app, app_token)
    health = HealthServer(
        socket_handler.client.is_connected, host=config.health_host, port=config.health_port
    )
    await server.start()
    try:
        await health.start()
        await socket_handler.start_async()
    finally:
        await health.stop()
        await socket_handler.close_async()
        await server.stop()
        if isinstance(handler.agent.llm, OpenAIClient):
            await handler.agent.llm.close()


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="pro-pilot Slack hub")
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    asyncio.run(run_with_shutdown(run(args.config)))
