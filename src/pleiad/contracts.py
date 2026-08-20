"""Shared JSON and immutable contract primitives below orchestration layers."""

from typing import Any

from pydantic import BaseModel, ConfigDict

type JsonObject = dict[str, Any]
type JsonSchema = dict[str, Any]


class ContractModel(BaseModel):
    """Immutable model base shared by non-LLM domain contracts."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
    )
