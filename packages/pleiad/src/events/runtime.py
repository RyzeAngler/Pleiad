from typing import Literal

from pydantic import Field

from pleiad.events.contracts import Event


class RuntimeEvent(Event):
    """Require a Run identity for every Runtime-owned event."""

    run_id: str = Field(min_length=1)


class AgentRuntimeEvent(RuntimeEvent):
    """Correlate a Runtime event with the active Agent."""

    agent_name: str = Field(min_length=1)


class RunStartedEvent(AgentRuntimeEvent):
    """Report the start of a new SDK Run."""

    kind: Literal["run.started"] = "run.started"


class RunResumedEvent(AgentRuntimeEvent):
    """Report continuation of an existing SDK Run."""

    kind: Literal["run.resumed"] = "run.resumed"


class RunCompletedEvent(RuntimeEvent):
    """Report successful completion of a Run."""

    kind: Literal["run.completed"] = "run.completed"


class RunFailedEvent(RuntimeEvent):
    """Report a business failure with safe public error details."""

    kind: Literal["run.failed"] = "run.failed"
    error_code: str = Field(min_length=1)
    error_message: str = Field(min_length=1)


class RunCancelledEvent(RuntimeEvent):
    """Report cancellation of a Run by its owner."""

    kind: Literal["run.cancelled"] = "run.cancelled"


class RunInterruptedEvent(RuntimeEvent):
    """Report a paused Run and its pending interruption identities."""

    kind: Literal["run.interrupted"] = "run.interrupted"
    interruption_ids: tuple[str, ...] = Field(min_length=1)


class RunTimedOutEvent(RuntimeEvent):
    """Report that a Run exhausted its deadline."""

    kind: Literal["run.timed_out"] = "run.timed_out"


class RunLimitReachedEvent(RuntimeEvent):
    """Report that a Run exhausted a configured execution limit."""

    kind: Literal["run.limit_reached"] = "run.limit_reached"


class TurnEvent(AgentRuntimeEvent):
    """Correlate a Runtime event with one Memory Turn."""

    turn_id: str = Field(min_length=1)


class TurnStartedEvent(TurnEvent):
    """Report creation of an active Memory Turn."""

    kind: Literal["turn.started"] = "turn.started"


class TurnCompletedEvent(TurnEvent):
    """Report durable acceptance of a complete Memory Turn."""

    kind: Literal["turn.completed"] = "turn.completed"


class ModelStartedEvent(TurnEvent):
    """Report the start of one real model attempt."""

    kind: Literal["model.started"] = "model.started"
    model: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    message_count: int = Field(ge=0)


class ModelCompletedEvent(TurnEvent):
    """Report completion metadata for one real model attempt."""

    kind: Literal["model.completed"] = "model.completed"
    model: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    finish_reason: str = Field(min_length=1)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)


class ModelFailedEvent(TurnEvent):
    """Report failure of one real model attempt."""

    kind: Literal["model.failed"] = "model.failed"
    model: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    error_code: str = Field(min_length=1)


class ToolEvent(RuntimeEvent):
    """Correlate Tool execution with its declaring Assistant message."""

    turn_id: str = Field(min_length=1)
    assistant_message_id: str = Field(min_length=1)
    call_id: str = Field(min_length=1)
    tool_code: str = Field(min_length=1)


class ToolStartedEvent(ToolEvent):
    """Report the start of an authorized Tool execution."""

    kind: Literal["tool.started"] = "tool.started"


class ToolCompletedEvent(ToolEvent):
    """Report a confirmed Tool result and its model-visible message."""

    kind: Literal["tool.completed"] = "tool.completed"
    result_message_id: str = Field(min_length=1)
    ok: bool


class ToolInterruptedEvent(ToolEvent):
    """Report a Tool action paused for an external decision."""

    kind: Literal["tool.interrupted"] = "tool.interrupted"
    interruption_id: str = Field(min_length=1)


class HandoffEvent(RuntimeEvent):
    """Correlate an Agent transition with its declaring Tool call."""

    source_agent: str = Field(min_length=1)
    target_agent: str = Field(min_length=1)
    call_id: str = Field(min_length=1)


class HandoffStartedEvent(HandoffEvent):
    """Report the start of an in-Run Agent handoff."""

    kind: Literal["handoff.started"] = "handoff.started"


class HandoffCompletedEvent(HandoffEvent):
    """Report completion of an in-Run Agent handoff."""

    kind: Literal["handoff.completed"] = "handoff.completed"
    transition_id: str = Field(min_length=1)
