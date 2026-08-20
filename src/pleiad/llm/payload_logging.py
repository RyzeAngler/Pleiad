"""Debug-only logging for provider-native LLM request and response payloads."""

import json
import logging
from collections.abc import Mapping, Sequence
from enum import Enum

from pleiad.llm.config import LLMProtocol, LLMProvider

type PayloadValue = None | bool | int | float | str | list[PayloadValue] | dict[str, PayloadValue]


def log_raw_request(
    logger: logging.Logger,
    *,
    request_id: str,
    protocol: LLMProtocol,
    provider: LLMProvider,
    body: object,
) -> None:
    """Write one provider-native request body when DEBUG logging is enabled."""
    if not logger.isEnabledFor(logging.DEBUG):
        return
    document = {
        "event": "llm_raw_request",
        "request_id": request_id,
        "protocol": protocol.value,
        "provider": provider.value,
        "body": _json_value(body),
    }
    logger.debug(f"{encode_payload(document)}")


def log_raw_response(
    logger: logging.Logger,
    *,
    request_id: str,
    protocol: LLMProtocol,
    provider: LLMProvider,
    body: object,
) -> None:
    """Write one provider-native response body when DEBUG logging is enabled."""
    if not logger.isEnabledFor(logging.DEBUG):
        return
    document = {
        "event": "llm_raw_response",
        "request_id": request_id,
        "protocol": protocol.value,
        "provider": provider.value,
        "body": _json_value(body),
    }
    logger.debug(f"{encode_payload(document)}")


def capture_payload(logger: logging.Logger, value: object) -> PayloadValue | None:
    """Materialize an SDK event only when raw DEBUG logging is active."""
    if not logger.isEnabledFor(logging.DEBUG):
        return None
    return _json_value(value)


def encode_payload(value: object) -> str:
    """Encode SDK models and native containers as readable Unicode JSON."""
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


def _json_value(value: object) -> PayloadValue:
    """Recursively convert SDK-specific values into JSON-compatible data."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Enum):
        return _json_value(value.value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _json_value(model_dump(mode="json", exclude_none=True))
        except TypeError:
            return _json_value(model_dump())
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_json_value(item) for item in value]
    if isinstance(value, bytes | bytearray):
        return bytes(value).decode("utf-8", errors="replace")
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict):
        return {
            str(key): _json_value(item)
            for key, item in attributes.items()
            if not str(key).startswith("_")
        }
    return str(value)
