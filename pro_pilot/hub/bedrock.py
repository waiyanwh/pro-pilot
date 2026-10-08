"""Thin Bedrock Converse client with the bounded retry policy."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable
from typing import Any

import boto3

from pro_pilot.protocol.tools import bedrock_tool_specs
from pro_pilot.spoke.redact import redact_text

from .llm import LLMUnavailable

LOGGER = logging.getLogger(__name__)


class BedrockClient:
    def __init__(
        self,
        *,
        region: str,
        model_id: str,
        max_output_tokens: int = 2000,
        temperature: float = 0,
        client: Any | None = None,
        random_fn: Callable[[], float] = random.random,
    ) -> None:
        self.region = region
        self.model_id = model_id
        self.max_output_tokens = max_output_tokens
        self.temperature = temperature
        self._client = client
        self._random = random_fn

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = boto3.client("bedrock-runtime", region_name=self.region)
        return self._client

    async def converse(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tool_names: list[str],
        cluster_ids: list[str],
        use_tools: bool = True,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "modelId": self.model_id,
            "system": [{"text": system}],
            "messages": messages,
            "inferenceConfig": {
                "maxTokens": self.max_output_tokens,
                "temperature": self.temperature,
            },
        }
        if use_tools:
            request["toolConfig"] = {"tools": bedrock_tool_specs(tool_names, cluster_ids)}
        for attempt, delay in enumerate((0, 1, 3)):
            if delay:
                await asyncio.sleep(delay + self._random() * 0.2 * delay)
            try:
                response = await asyncio.to_thread(self._get_client().converse, **request)
                return response
            except Exception as exc:  # noqa: BLE001 - botocore errors are runtime-specific
                error = getattr(exc, "response", {}).get("Error", {})
                code = error.get("Code")
                raw_message = error.get("Message", str(exc))
                clean_message, _ = redact_text(raw_message)
                # Bound the logged detail: an upstream error body is attacker/service
                # controlled and must never grow the log line or echo raw credentials.
                bounded_message = clean_message[:300]
                LOGGER.error(
                    "bedrock_converse_failed model=%s region=%s attempt=%s code=%s message=%s",
                    self.model_id,
                    self.region,
                    attempt + 1,
                    code,
                    bounded_message,
                )
                if (
                    code not in {"ThrottlingException", "ServiceUnavailableException"}
                    or attempt == 2
                ):
                    raise LLMUnavailable("Bedrock is unavailable") from exc
        raise LLMUnavailable("Bedrock is unavailable")
