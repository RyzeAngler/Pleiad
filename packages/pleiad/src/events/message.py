from typing import Literal

from pydantic import Field

from pleiad.events.runtime import AgentRuntimeEvent
from pleiad.messages.message import Message


class MessageEvent(AgentRuntimeEvent):
    """Correlate one message lifecycle with its Memory Turn."""

    turn_id: str = Field(min_length=1)


class MessageStartedEvent(MessageEvent):
    """Declare a message whose streamed content is beginning."""

    kind: Literal["message.started"] = "message.started"
    message_id: str = Field(min_length=1)
    role: Literal["system", "user", "assistant", "tool"]


class MessageTextDeltaEvent(MessageEvent):
    """Carry one actual Assistant text fragment."""

    kind: Literal["message.text.delta"] = "message.text.delta"
    message_id: str = Field(min_length=1)
    delta: str = Field(min_length=1)


class MessageToolCallDeltaEvent(MessageEvent):
    """Carry one fragment of a Tool call declared by an Assistant."""

    kind: Literal["message.tool_call.delta"] = "message.tool_call.delta"
    message_id: str = Field(min_length=1)
    index: int = Field(ge=0)
    id: str | None = Field(default=None, min_length=1)
    name: str | None = Field(default=None, min_length=1)
    arguments_delta: str = ""


class MessageRefusalDeltaEvent(MessageEvent):
    """Carry one Provider refusal fragment."""

    kind: Literal["message.refusal.delta"] = "message.refusal.delta"
    message_id: str = Field(min_length=1)
    delta: str = Field(min_length=1)


class MessageCompletedEvent(MessageEvent):
    """Carry the final accepted snapshot of one message."""

    kind: Literal["message.completed"] = "message.completed"
    message: Message


class MessageAbortedEvent(MessageEvent):
    """Tell consumers that an incomplete streamed message has terminated."""

    kind: Literal["message.aborted"] = "message.aborted"
    message_id: str = Field(min_length=1)
    reason: Literal["failed", "cancelled", "timed_out"]
