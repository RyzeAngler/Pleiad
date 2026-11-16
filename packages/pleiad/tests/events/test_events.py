from typing import Literal

import pytest
from pydantic import TypeAdapter, ValidationError

from pleiad.contracts import JsonObject, JsonValue
from pleiad.events.context import ContextCompactionLifecycleEvent, ContextCompactionStartedEvent
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


def compaction_event_data(kind: str) -> JsonObject:
    data: JsonObject = {
        "kind": kind,
        "compaction_id": "compaction-1",
        "run_id": "run-1",
        "step_id": "step-1",
        "agent_name": "assistant",
        "parent_run_id": "parent-run",
        "trigger_reasons": ["compact_at_reached"],
        "compact_at": 100,
        "available_tokens": 200,
        "before": {"input_tokens": 120, "estimated": True, "image_tokens": None, "unestimated_images": 1},
    }
    if kind != "context.compaction.started":
        data.update(
            duration_ms=12.5,
            strategies_attempted=["history_trim"],
            strategies_completed=["history_trim"],
        )
    if kind == "context.compaction.completed":
        data.update(
            after={"input_tokens": 40, "estimated": True, "image_tokens": None, "unestimated_images": 1},
            summary=None,
            retained_steps=[{"step_id": "old-2", "run_id": "old-run", "sequence": 2}],
            omitted_steps=[{"step_id": "old-1", "run_id": "old-run", "sequence": 1}],
            retained_context_blocks=[],
            removed_context_blocks=[],
            replaced_tool_result_ids=[],
        )
    if kind == "context.compaction.failed":
        data.update(error_code="context.compaction.failed", error_message="The history summary failed.")
    return data


@pytest.mark.parametrize("phase", ["started", "completed", "failed", "cancelled"])
def test_compaction_events_round_trip_with_attempt_and_request_identity(phase: str) -> None:
    adapter = TypeAdapter[ContextCompactionLifecycleEvent](ContextCompactionLifecycleEvent)
    event = adapter.validate_python(compaction_event_data(f"context.compaction.{phase}"))
    restored = adapter.validate_json(event.model_dump_json())

    assert restored == event
    assert restored.compaction_id == "compaction-1"
    assert (restored.run_id, restored.step_id, restored.agent_name) == ("run-1", "step-1", "assistant")
    assert restored.parent_run_id == "parent-run"
    assert restored.before.estimated and restored.before.image_tokens is None
    assert restored.before.unestimated_images == 1


@pytest.mark.parametrize("missing", ["compaction_id", "run_id", "step_id", "agent_name", "before"])
def test_compaction_started_rejects_missing_attempt_or_request_facts(missing: str) -> None:
    data = compaction_event_data("context.compaction.started")
    data.pop(missing)

    with pytest.raises(ValidationError, match=missing):
        ContextCompactionStartedEvent.model_validate(data)


@pytest.mark.parametrize("missing", ["error_code", "error_message"])
def test_compaction_failure_requires_public_error_details(missing: str) -> None:
    data = compaction_event_data("context.compaction.failed")
    data.pop(missing)

    with pytest.raises(ValidationError, match=missing):
        TypeAdapter(ContextCompactionLifecycleEvent).validate_python(data)


@pytest.mark.parametrize(
    "attempted,completed",
    [
        (["history_trim", "history_trim"], ["history_trim"]),
        (["history_trim"], ["history_trim", "history_trim"]),
        (["history_trim"], ["rolling_summary"]),
    ],
)
def test_compaction_terminal_rejects_repeated_or_unattempted_strategy_stages(
    attempted: list[JsonValue], completed: list[JsonValue]
) -> None:
    data = compaction_event_data("context.compaction.cancelled")
    data["strategies_attempted"] = attempted
    data["strategies_completed"] = completed

    with pytest.raises(ValidationError, match="strategies"):
        TypeAdapter(ContextCompactionLifecycleEvent).validate_python(data)


@pytest.mark.parametrize("kind", [None, "context.compaction.unknown"])
def test_compaction_event_union_rejects_missing_or_unknown_lifecycle_kind(kind: str | None) -> None:
    data = compaction_event_data("context.compaction.started")
    if kind is None:
        data.pop("kind")
    else:
        data["kind"] = kind

    with pytest.raises(ValidationError, match="kind"):
        TypeAdapter(ContextCompactionLifecycleEvent).validate_python(data)
