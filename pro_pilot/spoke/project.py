"""Project raw executor data into safe, small tool output models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

_FORBIDDEN_KEYS = {
    "managedFields",
    "kubectl.kubernetes.io/last-applied-configuration",
    "values",
    "valuesObject",
    "httpHeaders",
    "http_headers",
    "headers",
    "command",
    "spec",
    "stringData",
}


def strip_forbidden(value: Any, *, in_env: bool = False) -> Any:
    """Remove raw-object fields that must never cross the spoke boundary."""

    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key in _FORBIDDEN_KEYS:
                continue
            if in_env and key == "value":
                continue
            result[key] = strip_forbidden(item, in_env=(key == "env"))
        return result
    if isinstance(value, list):
        return [strip_forbidden(item, in_env=in_env) for item in value]
    return value


def project_output(output_model: type[BaseModel], raw: Any) -> Any:
    """Validate and serialize a projected output model, never passing raw objects through."""

    clean = strip_forbidden(raw)
    output = output_model.model_validate(clean)
    return output.model_dump(mode="json", by_alias=True, exclude_none=True)
