"""Provider-neutral Tool descriptors and execution contracts."""

from datetime import datetime
from typing import Self

from pydantic import Field, model_validator

from pleiad.contracts import ContractModel, JsonObject
from pleiad.errors import ErrorKind


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
    idempotent: bool = False


class ToolContext(ContractModel):
    """Non-secret execution context supplied by Runtime."""

    execution_id: str = Field(min_length=1)
    agent_run_id: str = Field(min_length=1)
    node_run_id: str | None = None
    permissions: frozenset[str] = frozenset()
    metadata: JsonObject = Field(default_factory=dict)


class ToolCall(ContractModel):
    """One model-requested Tool invocation."""

    call_id: str = Field(min_length=1)
    tool_code: str = Field(min_length=1)
    arguments: JsonObject = Field(default_factory=dict)


class ToolError(ContractModel):
    """Safe failure information returned to an Agent."""

    code: str = Field(default="system.unexpected_error", min_length=1)
    message: str = Field(default="系统发生未预期错误", min_length=1)
    kind: ErrorKind = ErrorKind.TOOL
    retryable: bool = False
    terminal: bool = False
    details: JsonObject = Field(default_factory=dict)


class ToolResult(ContractModel):
    """Normalized result of one Tool call."""

    call_id: str = Field(min_length=1)
    ok: bool
    output: JsonObject | None = None
    error: ToolError | None = None
    started_at: datetime
    finished_at: datetime

    @model_validator(mode="after")
    def validate_result_shape(self) -> Self:
        """Keep success and failure payloads mutually exclusive."""
        if self.ok and self.error is not None:
            raise ValueError("successful Tool results cannot contain an error")
        if not self.ok and self.error is None:
            raise ValueError("failed Tool results require an error")
        return self
