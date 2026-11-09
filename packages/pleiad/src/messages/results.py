import hashlib
import json

from pydantic import Field

from pleiad.contracts import ContractModel, JsonObject
from pleiad.messages.message import ToolMessage
from pleiad.tools.contracts import ToolResult

TOOL_RESULT_PAGE_CHARS = 4_000
MAX_TOOL_RESULT_PAGE_CHARS = 8_000
MAX_INLINE_TOOL_RESULT_CHARS = 64_000


class ResultReference(ContractModel):
    """Identify a host-authorized immutable result and optional bounded summary."""

    reference: str = Field(min_length=1, max_length=4096)
    digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    summary: str | None = Field(default=None, max_length=2_048)


class ToolResultReference(ContractModel):
    """Identify an immutable Tool observation in the current Session.

    The host owns the memory scope and its retention. A reference never grants
    access to another conversation or to an arbitrary filesystem location.
    """

    message_id: str = Field(min_length=1, max_length=256)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    total_chars: int = Field(ge=0)

    @classmethod
    def from_message(cls, message: ToolMessage) -> "ToolResultReference":
        """Describe the complete persisted observation without copying its body.

        Args:
            message: Original Tool message retained by Session.

        Returns:
            Its identity, UTF-8 digest, and character count.
        """
        return cls(
            message_id=message.message_id,
            sha256=hashlib.sha256(message.content.encode("utf-8")).hexdigest(),
            total_chars=len(message.content),
        )

    def read_arguments(self) -> JsonObject:
        """Return model-visible arguments for reading the first bounded page.

        Returns:
            Arguments accepted by the Run-scoped read_tool_result Tool.
        """
        return {"message_id": self.message_id, "start_char": 0, "max_chars": TOOL_RESULT_PAGE_CHARS}


class OperationResultReference(ContractModel):
    """Identify a complete persisted operation result without copying it into input."""

    operation_id: str = Field(min_length=1, max_length=256)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    total_chars: int = Field(ge=0)
    summary: str = Field(max_length=2_048)

    @classmethod
    def from_result(cls, operation_id: str, result: ToolResult) -> "OperationResultReference":
        """Describe actual final result bytes and a bounded discovery hint."""
        content = result.model_dump_json()
        return cls(
            operation_id=operation_id,
            sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            total_chars=len(content),
            summary=json.dumps(
                {
                    "ok": result.ok,
                    "preview": content[:128],
                    "read_tool_result": {
                        "operation_id": operation_id,
                        "start_char": 0,
                        "max_chars": TOOL_RESULT_PAGE_CHARS,
                    },
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )

    def read_arguments(self) -> JsonObject:
        """Return a selector for the first bounded page of this operation result."""
        return {"operation_id": self.operation_id, "start_char": 0, "max_chars": TOOL_RESULT_PAGE_CHARS}

    def input_reference(self) -> ResultReference:
        """Project this operation identity into its immutable bounded input content."""
        return ResultReference(
            reference=f"operation:{self.operation_id}:result", digest=self.sha256, summary=self.summary
        )
