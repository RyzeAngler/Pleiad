import pytest
from pydantic import ValidationError

from pleiad.memory.errors import MemoryTurnConflictError, MemoryTurnInvalidError
from pleiad.memory.turn import Turn
from pleiad.messages.message import AssistantMessage, Message, ToolCall, ToolMessage, UserMessage


def user(message_id: str = "user-1") -> UserMessage:
    return UserMessage(message_id=message_id, content="question")


def assistant(message_id: str = "assistant-1", calls: tuple[ToolCall, ...] = ()) -> AssistantMessage:
    return AssistantMessage(message_id=message_id, content="answer" if not calls else None, tool_calls=calls)


def result(message_id: str = "result-message-1", call_id: str = "call-1") -> ToolMessage:
    return ToolMessage(message_id=message_id, tool_call_id=call_id, content='{"ok": true}')


def test_turn_remains_active_until_an_explicit_completion() -> None:
    turn = Turn(turn_id="turn-1", sequence=1, agent_id="agent", messages=(user(),))

    assert not turn.completed
    assert turn.messages == (user(),)


def test_turn_completes_a_direct_assistant_answer() -> None:
    turn = Turn(turn_id="turn-1", sequence=1, agent_id="agent", messages=(user(),))

    completed = turn.complete((assistant(),))

    assert completed.completed
    assert [message.role for message in completed.messages] == ["user", "assistant"]


def test_turn_completes_nested_tool_calls_and_results_in_declaration_order() -> None:
    calls = (
        ToolCall(id="call-1", name="first", arguments="{}"),
        ToolCall(id="call-2", name="second", arguments="{}"),
    )
    turn = Turn(turn_id="turn-1", sequence=1, agent_id="agent", messages=(user(),))
    active = turn.append((assistant(calls=calls), result()))

    completed = active.complete((result("result-message-2", "call-2"),))

    assert completed.completed
    assert [message.role for message in completed.messages] == ["user", "assistant", "tool", "tool"]


def test_turn_rejects_completion_when_a_tool_result_is_missing() -> None:
    call = ToolCall(id="call-1", name="lookup", arguments="{}")
    turn = Turn(turn_id="turn-1", sequence=1, agent_id="agent", messages=(user(), assistant(calls=(call,))))

    with pytest.raises(MemoryTurnInvalidError, match="cannot be completed"):
        turn.complete()


def test_turn_rejects_unknown_or_out_of_order_tool_results() -> None:
    calls = (
        ToolCall(id="call-1", name="first", arguments="{}"),
        ToolCall(id="call-2", name="second", arguments="{}"),
    )
    with pytest.raises(ValidationError, match="declaration order"):
        Turn(
            turn_id="turn-1",
            sequence=1,
            agent_id="agent",
            messages=(user(), assistant(calls=calls), result(call_id="call-2")),
        )


def test_turn_retries_an_identical_message_without_duplicate_storage() -> None:
    turn = Turn(turn_id="turn-1", sequence=1, agent_id="agent")
    updated = turn.append((assistant(),))

    assert updated.append((assistant(),)) == updated


def test_turn_rejects_a_message_id_reused_with_different_content() -> None:
    turn = Turn(turn_id="turn-1", sequence=1, agent_id="agent").append((assistant(),))
    changed: Message = AssistantMessage(message_id="assistant-1", content="changed")

    with pytest.raises(MemoryTurnConflictError, match="different content"):
        turn.append((changed,))
