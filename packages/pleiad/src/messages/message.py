from typing import Annotated, Literal, Self
from uuid import uuid4

from pydantic import Field, TypeAdapter, field_validator, model_validator

from pleiad.contracts import ContractModel


class TextBlock(ContractModel):
    """Represent one provider-neutral text input block."""

    type: Literal["text"] = "text"
    text: str = Field(min_length=1, max_length=100_000)


class ToolCall(ContractModel):
    """Represent one Tool call declared by an Assistant message."""

    id: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=128)
    arguments: str = Field(max_length=100_000)


class MessageBase(ContractModel):
    """Provide stable identity shared by concrete message contracts."""

    message_id: str = Field(default_factory=lambda: uuid4().hex, min_length=1, max_length=256)


class SystemMessage(MessageBase):
    """Represent model instructions supplied through the system role."""

    role: Literal["system"] = "system"
    content: str = Field(min_length=1, max_length=100_000)

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        """Reject system instructions containing only whitespace."""
        if not value.strip():
            raise ValueError("system messages require non-empty content")
        return value


class UserMessage(MessageBase):
    """Represent user input accepted by the public Message boundary."""

    role: Literal["user"] = "user"
    content: str | tuple[TextBlock, ...]

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str | tuple[TextBlock, ...]) -> str | tuple[TextBlock, ...]:
        """Reject empty text and empty block collections."""
        if isinstance(value, str):
            if not value.strip():
                raise ValueError("user messages require non-empty content")
            return value
        if not value:
            raise ValueError("user messages require at least one content block")
        return value


class AssistantMessage(MessageBase):
    """Represent one complete Assistant response and its Tool declarations."""

    role: Literal["assistant"] = "assistant"
    content: str | None = Field(default=None, max_length=100_000)
    tool_calls: tuple[ToolCall, ...] = ()
    refusal: str | None = Field(default=None, max_length=100_000)

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        """Require output and unique call identities without parsing arguments."""
        has_content = self.content is not None and bool(self.content)
        has_refusal = self.refusal is not None and bool(self.refusal)
        if not has_content and not has_refusal and not self.tool_calls:
            raise ValueError("assistant messages require content, refusal, or Tool calls")
        call_ids = tuple(call.id for call in self.tool_calls)
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("assistant Tool call IDs must be unique")
        return self


class ToolMessage(MessageBase):
    """Represent the model-visible result for one declared Tool call."""

    role: Literal["tool"] = "tool"
    content: str
    tool_call_id: str = Field(min_length=1, max_length=256)


Message = Annotated[
    SystemMessage | UserMessage | AssistantMessage | ToolMessage,
    Field(discriminator="role"),
]
MESSAGE_ADAPTER: TypeAdapter[Message] = TypeAdapter(Message)
