from datetime import datetime
from typing import Self

from pydantic import Field, model_validator

from pleiad.contracts import CapabilityGrants, ContractModel, JsonObject, ToolRef
from pleiad.errors.base import ErrorType

DEFAULT_TOOL_CODES = ("read_file", "write_file", "command")


def default_tool_refs() -> tuple[ToolRef, ...]:
    """Return the default model-visible Tool references for every Agent."""
    return tuple(ToolRef(code) for code in DEFAULT_TOOL_CODES)


class ToolDescriptor(ContractModel):
    """The only Tool data written in the user-facing capability YAML."""

    name: str = Field(min_length=1, max_length=128)
    code: str = Field(min_length=1, max_length=256)
    description: str = Field(min_length=1, max_length=16_000)


class ToolPolicy(ContractModel):
    """Runtime-owned safety policy for one loaded Tool."""

    timeout_seconds: float = Field(default=60.0, gt=0.0, le=86_400.0)
    max_retries: int = Field(default=0, ge=0, le=10)
    max_concurrency: int = Field(default=1, ge=1, le=1_024)
    parallel_safe: bool = False
    idempotent: bool = False


class ParentRunLimits(ContractModel):
    """Remaining parent Run limits visible at a nested execution boundary."""

    deadline: datetime
    remaining_turns: int = Field(ge=0)
    remaining_output_tokens: int = Field(ge=0)
    tool_concurrency: int = Field(gt=0)


class ToolContext(ContractModel):
    """Non-secret execution context supplied by Runtime."""

    call_id: str = Field(min_length=1)
    run_id: str | None = Field(default=None, min_length=1)
    parent_run_id: str | None = Field(default=None, min_length=1)
    turn_id: str | None = Field(default=None, min_length=1)
    grants: CapabilityGrants = CapabilityGrants()
    parent_limits: ParentRunLimits | None = None
    metadata: JsonObject = Field(default_factory=dict)


class ToolCall(ContractModel):
    """One model-requested Tool invocation."""

    call_id: str = Field(min_length=1)
    tool_code: str = Field(min_length=1)
    arguments: JsonObject = Field(default_factory=dict)


class ToolResult(ContractModel):
    """Normalized result of one Tool call."""

    call_id: str = Field(min_length=1)
    ok: bool
    output: JsonObject | None = None
    error_code: str | None = Field(default=None, min_length=1)
    error_message: str | None = Field(default=None, min_length=1)
    error_type: ErrorType | None = None
    started_at: datetime
    finished_at: datetime

    @model_validator(mode="after")
    def validate_result_shape(self) -> Self:
        """Keep success and failure payloads mutually exclusive."""
        error_fields = (self.error_code, self.error_message, self.error_type)
        has_error = any(value is not None for value in error_fields)
        complete_error = all(value is not None for value in error_fields)
        if has_error and not complete_error:
            raise ValueError("Tool error fields must be provided together")
        if self.ok and has_error:
            raise ValueError("successful Tool results cannot contain an error")
        if self.ok and self.output is None:
            raise ValueError("successful Tool results require an object output")
        if not self.ok and not complete_error:
            raise ValueError("failed Tool results require an error")
        if self.error_type is not None and self.error_type is not ErrorType.BUSINESS:
            raise ValueError("Tool results can only contain business errors")
        if self.finished_at < self.started_at:
            raise ValueError("Tool result cannot finish before it starts")
        return self
