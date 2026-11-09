from typing import Literal, Self

from pydantic import ConfigDict, Field, ValidationError, model_validator

from pleiad.contracts import ContractModel, JsonObject, JsonSchema, JsonValue
from pleiad.messages.message import ToolMessage
from pleiad.messages.results import (
    MAX_TOOL_RESULT_PAGE_CHARS,
    TOOL_RESULT_PAGE_CHARS,
    OperationResultReference,
    ToolResultReference,
)
from pleiad.sessions.writer import SessionWriter
from pleiad.tools.base import BaseTool, ToolOutput
from pleiad.tools.contracts import ToolContext, ToolDescriptor, ToolPolicy
from pleiad.tools.errors import InvalidToolArgumentsError, ToolResultNotFoundError


class ReadToolResultArguments(ContractModel):
    """Request a character page from one completed Tool observation."""

    model_config = ConfigDict(strict=True)

    kind: Literal["tool_message", "operation"] = "tool_message"
    message_id: str | None = Field(default=None, min_length=1, max_length=256)
    operation_id: str | None = Field(default=None, min_length=1, max_length=256)
    after_message_id: str | None = Field(default=None, min_length=1, max_length=256)
    after_operation_id: str | None = Field(default=None, min_length=1, max_length=256)
    limit: int = Field(default=10, ge=1, le=20)
    start_char: int = Field(default=0, ge=0)
    max_chars: int = Field(default=TOOL_RESULT_PAGE_CHARS, ge=1, le=MAX_TOOL_RESULT_PAGE_CHARS)

    @model_validator(mode="after")
    def validate_mode(self) -> Self:
        """Keep reference enumeration and observation page cursors distinct."""
        if self.message_id is not None and self.operation_id is not None:
            raise ValueError("Select exactly one original message or operation result")
        reading = self.message_id is not None or self.operation_id is not None
        if reading and (self.after_message_id is not None or self.after_operation_id is not None):
            raise ValueError("Reference enumeration cursors cannot accompany result page selectors")
        if self.after_operation_id is not None and self.kind != "operation":
            raise ValueError("after_operation_id requires operation reference enumeration")
        if self.kind == "operation" and (self.message_id is not None or self.after_message_id is not None):
            raise ValueError("Operation references cannot use ToolMessage identities")
        if not reading and self.start_char != 0:
            raise ValueError("start_char requires a message_id or operation_id")
        return self


class ReadToolResultTool(BaseTool):
    """Provide a Runtime-owned reader for the current Run's original observations.

    Runtime binds this Tool per invocation. The model cannot select a Session ID,
    database, or host path. Reopening the same host-authorized SQLite Session
    preserves references across process restarts.
    """

    def __init__(self, writer: SessionWriter) -> None:
        """Bind the already authorized Session writer.

        Args:
            writer: SessionWriter supplied by the host for this Run.
        """
        self.writer = writer

    @property
    def descriptor(self) -> ToolDescriptor:
        """Return the reserved Runtime Tool identity."""
        return ToolDescriptor(
            name="read_tool_result",
            code="read_tool_result",
            description=(
                "Read original results from this Session. Select message_id for an original ToolMessage "
                "or operation_id for a completed background operation. Use start_char/max_chars for bounded pages. "
                "Omit both IDs to list ToolMessage references, or set kind=operation to list completed operations."
            ),
        )

    @property
    def input_schema(self) -> JsonSchema:
        """Return the validated reference and pagination input schema."""
        return ReadToolResultArguments.model_json_schema()

    @property
    def policy(self) -> ToolPolicy:
        """Permit bounded, independent reads without changing history."""
        return ToolPolicy(max_concurrency=16, parallel_safe=True, idempotent=True)

    async def execute(self, context: ToolContext, arguments: JsonObject) -> ToolOutput:
        """Read a page and return the digest of its complete original observation.

        Args:
            context: Identity of the current Tool invocation.
            arguments: Original message or operation identity and a bounded character window.

        Returns:
            The content page, reference, and exclusive next character offset.

        Raises:
            ToolResultNotFoundError: If this Session has no completed result with that ID.
            InvalidToolArgumentsError: If the requested offset exceeds the original body.
        """
        try:
            parsed = ReadToolResultArguments.model_validate(arguments)
        except ValidationError as error:
            raise InvalidToolArgumentsError("The result reference or pagination selector is invalid.") from error
        if parsed.operation_id is not None:
            journal = await self.writer.store.snapshot(self.writer.session_id)
            operation = next(
                (
                    value
                    for value in journal.operations
                    if value.operation_id == parsed.operation_id and value.state in {"succeeded", "failed", "cancelled"}
                ),
                None,
            )
            if operation is None or operation.result is None:
                raise ToolResultNotFoundError()
            reference: ToolResultReference | OperationResultReference = OperationResultReference.from_result(
                operation.operation_id, operation.result
            )
            content = operation.result.model_dump_json()
        elif parsed.message_id is not None:
            message = await self.writer.read_tool_result(parsed.message_id)
            if message is None:
                raise ToolResultNotFoundError()
            reference = ToolResultReference.from_message(message)
            content = message.content
        elif parsed.kind == "operation":
            return await self.list_operations(parsed.after_operation_id, parsed.limit)
        else:
            return await self.list_references(parsed.after_message_id, parsed.limit)
        if parsed.start_char > reference.total_chars:
            raise InvalidToolArgumentsError("start_char exceeds the Tool result length.")
        end = min(parsed.start_char + parsed.max_chars, reference.total_chars)
        return {
            "reference": reference.model_dump(mode="json"),
            "start_char": parsed.start_char,
            "next_start_char": end,
            "content": content[parsed.start_char : end],
            "truncated": end < reference.total_chars,
        }

    async def list_references(self, after_message_id: str | None, limit: int) -> JsonObject:
        """List bounded references even when their complete ModelSteps left the request view.

        Args:
            after_message_id: Exclusive cursor from a previous reference page.
            limit: Maximum number of references to return.

        Returns:
            Newest-first references with previews and an optional continuation cursor.

        Raises:
            InvalidToolArgumentsError: If the cursor is outside this memory's completed history.
        """
        messages = [
            message
            for step in reversed(await self.writer.read())
            for message in reversed(step.messages)
            if isinstance(message, ToolMessage)
        ]
        start = 0
        if after_message_id is not None:
            positions = [index for index, message in enumerate(messages) if message.message_id == after_message_id]
            if not positions:
                raise InvalidToolArgumentsError("The reference cursor is not available in this conversation.")
            start = positions[0] + 1
        selected = messages[start : start + limit]
        references: list[JsonValue] = [
            {
                **ToolResultReference.from_message(message).model_dump(mode="json"),
                "tool_call_id": message.tool_call_id,
                "preview": message.content[:128],
            }
            for message in selected
        ]
        return {
            "references": references,
            "next_message_id": selected[-1].message_id if selected and start + len(selected) < len(messages) else None,
        }

    async def list_operations(self, after_operation_id: str | None, limit: int) -> JsonObject:
        """List final operation references in this Session without copying result bodies."""
        journal = await self.writer.store.snapshot(self.writer.session_id)
        operations = [
            value
            for value in reversed(journal.operations)
            if value.result is not None and value.state in {"succeeded", "failed", "cancelled"}
        ]
        start = 0
        if after_operation_id is not None:
            positions = [index for index, value in enumerate(operations) if value.operation_id == after_operation_id]
            if not positions:
                raise InvalidToolArgumentsError("The operation cursor is not available in this Session.")
            start = positions[0] + 1
        selected = operations[start : start + limit]
        references: list[JsonValue] = []
        for operation in selected:
            if operation.result is None:
                raise ValueError("A final operation reference requires its persisted result")
            references.append(
                OperationResultReference.from_result(operation.operation_id, operation.result).model_dump(mode="json")
            )
        return {
            "references": references,
            "next_operation_id": selected[-1].operation_id
            if selected and start + len(selected) < len(operations)
            else None,
        }
