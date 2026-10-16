from pydantic import Field, ValidationError, model_validator

from pleiad.contracts import ContractModel
from pleiad.memory.errors import MemoryTurnConflictError, MemoryTurnInvalidError
from pleiad.messages.message import AssistantMessage, Message, SystemMessage, ToolMessage, UserMessage


class Turn(ContractModel):
    """Represent one model call and the Tool results it produces."""

    turn_id: str = Field(min_length=1, max_length=256)
    sequence: int = Field(ge=1)
    agent_id: str = Field(min_length=1, max_length=256)
    completed: bool = False
    messages: tuple[Message, ...] = ()

    @model_validator(mode="after")
    def validate_turn(self) -> "Turn":
        """Validate message order, Tool pairing, and completion state."""
        message_ids = tuple(message.message_id for message in self.messages)
        if len(message_ids) != len(set(message_ids)):
            raise ValueError("Turn message IDs must be unique")
        if any(isinstance(message, SystemMessage) for message in self.messages):
            raise ValueError("system messages do not belong to a Turn")

        user_positions = tuple(index for index, message in enumerate(self.messages) if isinstance(message, UserMessage))
        if user_positions and user_positions != (0,):
            raise ValueError("a user message can only be the first Turn message")

        assistant_positions = tuple(
            index for index, message in enumerate(self.messages) if isinstance(message, AssistantMessage)
        )
        if len(assistant_positions) > 1:
            raise ValueError("a Turn can contain only one assistant message")
        assistant_index = assistant_positions[0] if assistant_positions else None
        if assistant_index is None:
            if any(isinstance(message, ToolMessage) for message in self.messages):
                raise ValueError("Tool messages require an Assistant message")
            if self.completed:
                raise ValueError("a completed Turn requires an assistant message")
            return self

        for message in self.messages[:assistant_index]:
            if not isinstance(message, UserMessage):
                raise ValueError("only a user message can precede the assistant message")

        assistant = self.messages[assistant_index]
        if not isinstance(assistant, AssistantMessage):
            raise ValueError("Turn assistant position must contain an Assistant message")
        results: list[ToolMessage] = []
        for message in self.messages[assistant_index + 1 :]:
            if isinstance(message, ToolMessage):
                results.append(message)
            else:
                raise ValueError("only Tool messages can follow the Assistant message")

        call_ids = tuple(call.id for call in assistant.tool_calls)
        result_ids = tuple(message.tool_call_id for message in results)
        if len(result_ids) != len(set(result_ids)):
            raise ValueError("Turn Tool result IDs must be unique")
        for result in results:
            if result.tool_call_id not in call_ids:
                raise ValueError("every Tool result must match a Tool call")
        if result_ids != call_ids[: len(result_ids)]:
            raise ValueError("Tool results must follow Tool call declaration order")
        if self.completed and len(results) != len(call_ids):
            raise ValueError("a completed Turn requires one result for every Tool call")
        return self

    def append(self, messages: tuple[Message, ...]) -> "Turn":
        """Append a new ordered message batch with idempotent retry semantics.

        Args:
            messages: Non-empty messages to append to this Turn.

        Returns:
            The updated Turn, or this Turn when every message is an identical retry.

        Raises:
            MemoryTurnConflictError: If an ID has different content or a completed Turn would change.
            MemoryTurnInvalidError: If the batch is empty or breaks Turn invariants.
        """
        if not messages:
            raise MemoryTurnInvalidError("Turn append messages must not be empty.")
        known = {message.message_id: message for message in self.messages}
        added: list[Message] = []
        for message in messages:
            previous = known.get(message.message_id)
            if previous is not None:
                if previous != message:
                    raise MemoryTurnConflictError("The message ID is already used with different content.")
                continue
            known[message.message_id] = message
            added.append(message)
        if not added:
            return self
        if self.completed:
            raise MemoryTurnConflictError("A completed Turn cannot receive new messages.")
        try:
            return Turn(
                turn_id=self.turn_id,
                sequence=self.sequence,
                agent_id=self.agent_id,
                completed=False,
                messages=(*self.messages, *added),
            )
        except ValidationError as error:
            raise MemoryTurnInvalidError("The appended messages break Turn ordering or pairing.") from error

    def complete(self, messages: tuple[Message, ...] = ()) -> "Turn":
        """Complete this Turn after appending an optional final message batch.

        Args:
            messages: Optional final messages, normally Tool results or an Assistant message.

        Returns:
            The completed Turn.

        Raises:
            MemoryTurnConflictError: If a completed Turn would be changed.
            MemoryTurnInvalidError: If the resulting Turn is incomplete or invalid.
        """
        updated = self if not messages else self.append(messages)
        if updated.completed:
            return updated
        try:
            return Turn(
                turn_id=updated.turn_id,
                sequence=updated.sequence,
                agent_id=updated.agent_id,
                completed=True,
                messages=updated.messages,
            )
        except ValidationError as error:
            raise MemoryTurnInvalidError("The Turn cannot be completed with its current messages.") from error
