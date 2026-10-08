"""Shared JSON stdout logging with redaction for standard-library loggers."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

import structlog
from structlog.typing import EventDict

from pro_pilot.spoke.redact import redact


def _safe_event(logger: Any, method: str, event: EventDict) -> EventDict:
    message = event.get("event")
    if isinstance(message, str):
        try:
            record = json.loads(message)
        except (ValueError, TypeError):
            record = None
        if isinstance(record, dict) and "event" in record:
            event.update(record)
    safe, _ = redact(event)
    return safe


def configure_logging() -> None:
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[
            structlog.stdlib.ExtraAdder(),
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
            structlog.processors.format_exc_info,
        ],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            _safe_event,
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
