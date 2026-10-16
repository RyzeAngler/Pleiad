from collections.abc import Mapping
from uuid import uuid4

from pydantic import Field, field_validator

from pleiad.contracts import ContractModel, JsonObject, freeze_json_object, thaw_json_object
from pleiad.tools.contracts import ToolResult


class ToolInterruption(ContractModel):
    """Serializable request for host action before a Tool call can finish."""

    interruption_id: str = Field(default_factory=lambda: uuid4().hex, min_length=1)
    call_id: str = Field(min_length=1)
    tool_code: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    arguments: JsonObject
    reason: str = Field(min_length=1)
    details: JsonObject = Field(default_factory=dict)

    @field_validator("arguments", "details", mode="before")
    @classmethod
    def validate_json(cls, value: object) -> JsonObject:
        """Detach mappings and reject values outside the shared JSON contract."""
        if not isinstance(value, Mapping):
            raise ValueError("interruption JSON fields must be objects")
        return thaw_json_object(freeze_json_object(value))


class InterruptionResponse(ContractModel):
    """Host-supplied final Tool result for one pending interruption."""

    interruption_id: str = Field(min_length=1)
    result: ToolResult


type ToolExecutionOutcome = ToolResult | ToolInterruption
