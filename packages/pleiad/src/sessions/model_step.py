from typing import Literal

from pydantic import Field, ValidationError, model_validator

from pleiad.contracts import ContractModel
from pleiad.messages.message import AssistantMessage, Message, SystemMessage, ToolMessage, UserMessage
from pleiad.sessions.errors import ModelStepConflictError, ModelStepInvalidError


class ModelStep(ContractModel):
    """Represent one model call and the Tool results it produces."""

    step_id: str = Field(min_length=1, max_length=256)
    run_id: str = Field(min_length=1, max_length=256)
    sequence: int = Field(ge=1)
    run_sequence: int = Field(ge=1)
    agent_id: str = Field(min_length=1, max_length=256)
    input_ids: tuple[str, ...] = ()
    state: Literal["active", "completed", "aborted"] = "active"
    messages: tuple[Message, ...] = ()

    @property
    def completed(self) -> bool:
        """Return whether the complete response and tool pairing were committed."""
        return self.state == "completed"

    @model_validator(mode="after")
    def validate_step(self) -> "ModelStep":
        """Validate message order, Tool pairing, and completion state."""
        message_ids = tuple(message.message_id for message in self.messages)
        if len(message_ids) != len(set(message_ids)):
            raise ValueError("ModelStep message IDs must be unique")
        if len(self.input_ids) != len(set(self.input_ids)):
            raise ValueError("ModelStep input IDs must be unique")
        if any(isinstance(message, SystemMessage) for message in self.messages):
            raise ValueError("system messages do not belong to a ModelStep")

        assistant_positions = tuple(
            index for index, message in enumerate(self.messages) if isinstance(message, AssistantMessage)
        )
        if len(assistant_positions) > 1:
            raise ValueError("a ModelStep can contain only one assistant message")
        assistant_index = assistant_positions[0] if assistant_positions else None
        if assistant_index is None:
            if any(isinstance(message, ToolMessage) for message in self.messages):
                raise ValueError("Tool messages require an Assistant message")
            if self.completed:
                raise ValueError("a completed ModelStep requires an assistant message")
            return self

        for message in self.messages[:assistant_index]:
            if not isinstance(message, UserMessage):
                raise ValueError("only a user message can precede the assistant message")

        assistant = self.messages[assistant_index]
        if not isinstance(assistant, AssistantMessage):
            raise ValueError("ModelStep assistant position must contain an Assistant message")
        results: list[ToolMessage] = []
        for message in self.messages[assistant_index + 1 :]:
            if isinstance(message, ToolMessage):
                results.append(message)
            else:
                raise ValueError("only Tool messages can follow the Assistant message")

        call_ids = tuple(call.id for call in assistant.tool_calls)
        result_ids = tuple(message.tool_call_id for message in results)
        if len(result_ids) != len(set(result_ids)):
            raise ValueError("ModelStep Tool result IDs must be unique")
        for result in results:
            if result.tool_call_id not in call_ids:
                raise ValueError("every Tool result must match a Tool call")
        if result_ids != call_ids[: len(result_ids)]:
            raise ValueError("Tool results must follow Tool call declaration order")
        if self.completed and len(results) != len(call_ids):
            raise ValueError("a completed ModelStep requires one result for every Tool call")
        return self

    def append(self, messages: tuple[Message, ...]) -> "ModelStep":
        """Append a new ordered message batch with idempotent retry semantics.

        Args:
            messages: Non-empty messages to append to this ModelStep.

        Returns:
            The updated ModelStep, or this ModelStep when every message is an identical retry.

        Raises:
            ModelStepConflictError: If an ID has different content or a completed ModelStep would change.
            ModelStepInvalidError: If the batch is empty or breaks ModelStep invariants.
        """
        if not messages:
            raise ModelStepInvalidError("ModelStep append messages must not be empty.")
        known = {message.message_id: message for message in self.messages}
        added: list[Message] = []
        for message in messages:
            previous = known.get(message.message_id)
            if previous is not None:
                if previous != message:
                    raise ModelStepConflictError("The message ID is already used with different content.")
                continue
            known[message.message_id] = message
            added.append(message)
        if not added:
            return self
        if self.state != "active":
            raise ModelStepConflictError("A settled ModelStep cannot receive new messages.")
        try:
            return ModelStep(
                step_id=self.step_id,
                run_id=self.run_id,
                sequence=self.sequence,
                run_sequence=self.run_sequence,
                agent_id=self.agent_id,
                input_ids=self.input_ids,
                messages=(*self.messages, *added),
            )
        except ValidationError as error:
            raise ModelStepInvalidError("The appended messages break ModelStep ordering or pairing.") from error

    def complete(self, messages: tuple[Message, ...] = ()) -> "ModelStep":
        """Complete this ModelStep after appending an optional final message batch.

        Args:
            messages: Optional final messages, normally Tool results or an Assistant message.

        Returns:
            The completed ModelStep.

        Raises:
            ModelStepConflictError: If a completed ModelStep would be changed.
            ModelStepInvalidError: If the resulting ModelStep is incomplete or invalid.
        """
        updated = self if not messages else self.append(messages)
        if updated.completed:
            return updated
        if updated.state == "aborted":
            raise ModelStepConflictError("An aborted ModelStep cannot be completed.")
        try:
            return ModelStep(
                step_id=updated.step_id,
                run_id=updated.run_id,
                sequence=updated.sequence,
                run_sequence=updated.run_sequence,
                agent_id=updated.agent_id,
                input_ids=updated.input_ids,
                state="completed",
                messages=updated.messages,
            )
        except ValidationError as error:
            raise ModelStepInvalidError("The ModelStep cannot be completed with its current messages.") from error
