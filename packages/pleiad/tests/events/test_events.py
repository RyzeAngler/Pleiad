from typing import Literal

import pytest
from pydantic import ValidationError

from pleiad.events.contracts import Event
from pleiad.events.graph import GraphEvent, GraphEventKind
from pleiad.events.message import MessageCompletedEvent, MessageToolCallDeltaEvent
from pleiad.events.runtime import ModelStepStartedEvent, RunFailedEvent, ToolCompletedEvent
from pleiad.messages.message import AssistantMessage


def test_application_event_can_extend_the_public_envelope_without_registration() -> None:
    class ProductEvent(Event):
        kind: Literal["product.notice"] = "product.notice"
        execution_id: str

    event = ProductEvent(execution_id="execution-1")

    assert event.kind == "product.notice"
    assert event.run_id is None


def test_runtime_failure_requires_safe_error_details_and_run_identity() -> None:
    with pytest.raises(ValidationError):
        RunFailedEvent.model_validate({"run_id": "run-1"})
    with pytest.raises(ValidationError):
        RunFailedEvent.model_validate({"error_code": "failed", "error_message": "safe"})


def test_step_event_round_trip_requires_the_new_identity_contract() -> None:
    event = ModelStepStartedEvent(run_id="run-1", step_id="step-1", agent_name="agent")
    restored = ModelStepStartedEvent.model_validate_json(event.model_dump_json())

    assert restored.kind == "step.started"
    assert restored.step_id == "step-1"
    legacy = event.model_dump(mode="json")
    legacy["turn_id"] = legacy.pop("step_id")
    with pytest.raises(ValidationError, match="step_id"):
        ModelStepStartedEvent.model_validate(legacy)


def test_message_completion_carries_the_complete_snapshot_without_duplicate_identity() -> None:
    message = AssistantMessage(message_id="message-1", content="answer")

    event = MessageCompletedEvent(
        run_id="run-1",
        step_id="step-1",
        agent_name="agent",
        message=message,
    )

    assert event.message.message_id == "message-1"
    assert "message_id" not in event.model_fields_set


def test_tool_call_delta_can_correlate_by_index_before_call_identity_arrives() -> None:
    event = MessageToolCallDeltaEvent(
        run_id="run-1",
        step_id="step-1",
        agent_name="agent",
        message_id="message-1",
        index=0,
        arguments_delta='{"city"',
    )

    assert event.id is None
    assert event.name is None


def test_tool_completion_requires_message_and_assistant_correlation() -> None:
    with pytest.raises(ValidationError):
        ToolCompletedEvent.model_validate(
            {
                "run_id": "run-1",
                "step_id": "step-1",
                "call_id": "call-1",
                "tool_code": "weather",
                "ok": True,
            }
        )


def test_graph_node_event_requires_node_identity_and_does_not_require_a_run() -> None:
    event = GraphEvent(
        kind=GraphEventKind.NODE_STARTED,
        graph_id="graph-1",
        revision=2,
        node_id="node-1",
    )

    assert event.run_id is None
    with pytest.raises(ValidationError, match="node_id"):
        GraphEvent(kind=GraphEventKind.NODE_COMPLETED, graph_id="graph-1", revision=2)
