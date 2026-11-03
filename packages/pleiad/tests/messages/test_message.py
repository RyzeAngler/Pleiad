import pytest
from pydantic import TypeAdapter, ValidationError

from pleiad.messages.message import (
    AssistantMessage,
    Message,
    SystemMessage,
    ToolCall,
    ToolMessage,
    UserMessage,
)


def test_user_message_generates_a_stable_identity() -> None:
    first = UserMessage(content="hello")
    second = UserMessage(content="hello")

    assert first.message_id
    assert first.message_id != second.message_id


def test_message_union_selects_the_concrete_role() -> None:
    message: Message = TypeAdapter(Message).validate_python({"role": "tool", "tool_call_id": "call-1", "content": ""})

    assert isinstance(message, ToolMessage)


def test_assistant_message_accepts_tool_only_output_and_raw_invalid_arguments() -> None:
    message = AssistantMessage(tool_calls=(ToolCall(id="call-1", name="lookup", arguments="{invalid"),))

    assert message.content is None
    assert message.tool_calls[0].arguments == "{invalid"


def test_assistant_message_accepts_a_provider_refusal() -> None:
    message = AssistantMessage(refusal="I cannot help with that.")

    assert message.refusal == "I cannot help with that."


def test_assistant_message_rejects_duplicate_call_ids() -> None:
    with pytest.raises(ValidationError, match="must be unique"):
        AssistantMessage(
            tool_calls=(
                ToolCall(id="call-1", name="first", arguments="{}"),
                ToolCall(id="call-1", name="second", arguments="{}"),
            )
        )


def test_user_message_accepts_only_strict_text_blocks() -> None:
    message = UserMessage.model_validate({"content": ({"type": "text", "text": "attached"},)})

    assert not isinstance(message.content, str)
    assert message.content[0].text == "attached"

    with pytest.raises(ValidationError, match="Input should be 'text'"):
        UserMessage.model_validate({"content": ({"type": "image", "url": "secret"},)})


def test_role_specific_messages_reject_foreign_fields() -> None:
    with pytest.raises(ValidationError, match="extra"):
        UserMessage.model_validate({"content": "hello", "tool_calls": ()})
    with pytest.raises(ValidationError, match="extra"):
        ToolMessage.model_validate({"tool_call_id": "call-1", "content": "ok", "refusal": "no"})


def test_system_message_rejects_whitespace_instructions() -> None:
    with pytest.raises(ValidationError, match="non-empty"):
        SystemMessage(content="   ")
