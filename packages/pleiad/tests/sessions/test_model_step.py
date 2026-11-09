import pytest
from pydantic import ValidationError

from pleiad.messages.message import AssistantMessage, Message, ToolCall, ToolMessage, UserMessage
from pleiad.sessions.errors import ModelStepConflictError, ModelStepInvalidError
from pleiad.sessions.model_step import ModelStep


def user(message_id: str = "user-1") -> UserMessage:
    return UserMessage(message_id=message_id, content="question")


def assistant(message_id: str = "assistant-1", calls: tuple[ToolCall, ...] = ()) -> AssistantMessage:
    return AssistantMessage(message_id=message_id, content="answer" if not calls else None, tool_calls=calls)


def result(message_id: str = "result-message-1", call_id: str = "call-1") -> ToolMessage:
    return ToolMessage(message_id=message_id, tool_call_id=call_id, content='{"ok": true}')


def test_step_remains_active_until_an_explicit_completion() -> None:
    step = ModelStep(run_id="run", run_sequence=1, step_id="step-1", sequence=1, agent_id="agent", messages=(user(),))

    assert not step.completed
    assert step.messages == (user(),)


def test_step_completes_a_direct_assistant_answer() -> None:
    step = ModelStep(run_id="run", run_sequence=1, step_id="step-1", sequence=1, agent_id="agent", messages=(user(),))

    completed = step.complete((assistant(),))

    assert completed.completed
    assert [message.role for message in completed.messages] == ["user", "assistant"]


def test_step_preserves_multiple_inputs_before_its_single_assistant_response() -> None:
    inputs = (user(), user("user-2"))
    step = ModelStep(run_id="run", run_sequence=1, step_id="step-1", sequence=1, agent_id="agent", messages=inputs)

    completed = step.complete((assistant(),))

    assert completed.completed
    assert completed.messages == (*inputs, assistant())


def test_step_rejects_user_input_after_its_assistant_response() -> None:
    step = ModelStep(
        run_id="run", run_sequence=1, step_id="step-1", sequence=1, agent_id="agent", messages=(user(), assistant())
    )

    with pytest.raises(ModelStepInvalidError, match="ordering or pairing"):
        step.append((user("user-2"),))


def test_step_completes_nested_tool_calls_and_results_in_declaration_order() -> None:
    calls = (
        ToolCall(id="call-1", name="first", arguments="{}"),
        ToolCall(id="call-2", name="second", arguments="{}"),
    )
    step = ModelStep(run_id="run", run_sequence=1, step_id="step-1", sequence=1, agent_id="agent", messages=(user(),))
    active = step.append((assistant(calls=calls), result()))

    completed = active.complete((result("result-message-2", "call-2"),))

    assert completed.completed
    assert [message.role for message in completed.messages] == ["user", "assistant", "tool", "tool"]


def test_step_rejects_completion_when_a_tool_result_is_missing() -> None:
    call = ToolCall(id="call-1", name="lookup", arguments="{}")
    step = ModelStep(
        run_id="run",
        run_sequence=1,
        step_id="step-1",
        sequence=1,
        agent_id="agent",
        messages=(user(), assistant(calls=(call,))),
    )

    with pytest.raises(ModelStepInvalidError, match="cannot be completed"):
        step.complete()


def test_step_rejects_unknown_or_out_of_order_tool_results() -> None:
    calls = (
        ToolCall(id="call-1", name="first", arguments="{}"),
        ToolCall(id="call-2", name="second", arguments="{}"),
    )
    with pytest.raises(ValidationError, match="declaration order"):
        ModelStep(
            run_id="run",
            run_sequence=1,
            step_id="step-1",
            sequence=1,
            agent_id="agent",
            messages=(user(), assistant(calls=calls), result(call_id="call-2")),
        )


def test_step_retries_an_identical_message_without_duplicate_storage() -> None:
    step = ModelStep(run_id="run", run_sequence=1, step_id="step-1", sequence=1, agent_id="agent")
    updated = step.append((assistant(),))

    assert updated.append((assistant(),)) == updated


def test_step_rejects_a_message_id_reused_with_different_content() -> None:
    step = ModelStep(run_id="run", run_sequence=1, step_id="step-1", sequence=1, agent_id="agent").append(
        (assistant(),)
    )
    changed: Message = AssistantMessage(message_id="assistant-1", content="changed")

    with pytest.raises(ModelStepConflictError, match="different content"):
        step.append((changed,))
