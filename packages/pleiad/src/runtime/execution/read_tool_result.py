from typing import Self

from pydantic import ConfigDict, Field, model_validator

from pleiad.contracts import ContractModel, JsonObject, JsonSchema, JsonValue
from pleiad.memory.short_memory import ShortMemory
from pleiad.messages.message import ToolMessage
from pleiad.messages.results import MAX_TOOL_RESULT_PAGE_CHARS, TOOL_RESULT_PAGE_CHARS, ToolResultReference
from pleiad.tools.base import BaseTool, ToolOutput
from pleiad.tools.contracts import ToolContext, ToolDescriptor, ToolPolicy
from pleiad.tools.errors import InvalidToolArgumentsError, ToolResultNotFoundError


class ReadToolResultArguments(ContractModel):
    """Request a character page from one completed Tool observation."""

    model_config = ConfigDict(strict=True)

    message_id: str | None = Field(default=None, min_length=1, max_length=256)
    after_message_id: str | None = Field(default=None, min_length=1, max_length=256)
    limit: int = Field(default=10, ge=1, le=20)
    start_char: int = Field(default=0, ge=0)
    max_chars: int = Field(default=TOOL_RESULT_PAGE_CHARS, ge=1, le=MAX_TOOL_RESULT_PAGE_CHARS)

    @model_validator(mode="after")
    def validate_mode(self) -> Self:
        """Keep reference enumeration and observation page cursors distinct."""
        if self.message_id is not None and self.after_message_id is not None:
            raise ValueError("after_message_id is only valid when listing references")
        if self.message_id is None and self.start_char != 0:
            raise ValueError("start_char requires message_id")
        return self


class ReadToolResultTool(BaseTool):
    """Provide a Runtime-owned reader for the current Run's original observations.

    Runtime binds this Tool per invocation. The model cannot select a memory ID,
    database, or host path. Reopening the same host-authorized SQLite memory
    preserves references across process restarts.
    """

    def __init__(self, memory: ShortMemory) -> None:
        """Bind the already authorized memory handle.

        Args:
            memory: ShortMemory supplied by the host for this Run.
        """
        self.memory = memory

    @property
    def descriptor(self) -> ToolDescriptor:
        """Return the reserved Runtime Tool identity."""
        return ToolDescriptor(
            name="read_tool_result",
            code="read_tool_result",
            description=(
                "Read original Tool results from this conversation. Omit message_id to list references, "
                "or supply message_id and start_char/max_chars to read a bounded page."
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
            arguments: Message identity and bounded character window.

        Returns:
            The content page, reference, and exclusive next character offset.

        Raises:
            ToolResultNotFoundError: If this memory has no completed result with that ID.
            InvalidToolArgumentsError: If the requested offset exceeds the original body.
        """
        parsed = ReadToolResultArguments.model_validate(arguments)
        if parsed.message_id is None:
            return await self.list_references(parsed.after_message_id, parsed.limit)
        message = await self.memory.read_tool_result(parsed.message_id)
        if message is None:
            raise ToolResultNotFoundError()
        reference = ToolResultReference.from_message(message)
        if parsed.start_char > reference.total_chars:
            raise InvalidToolArgumentsError("start_char exceeds the Tool result length.")
        end = min(parsed.start_char + parsed.max_chars, reference.total_chars)
        return {
            "reference": reference.model_dump(mode="json"),
            "start_char": parsed.start_char,
            "next_start_char": end,
            "content": message.content[parsed.start_char : end],
            "truncated": end < reference.total_chars,
        }

    async def list_references(self, after_message_id: str | None, limit: int) -> JsonObject:
        """List bounded references even when their complete Turns left the request view.

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
            for turn in reversed(await self.memory.read())
            for message in reversed(turn.messages)
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
